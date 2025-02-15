import logging
import os
from typing import Any, Dict, List, Optional, Sequence, Union
from uuid import UUID, uuid4

from langchain.callbacks.base import BaseCallbackHandler

from langfuse.api.resources.commons.types.observation_level import ObservationLevel
from langfuse.api.resources.ingestion.types.sdk_log_body import SdkLogBody
from langfuse.client import (
    Langfuse,
    StatefulSpanClient,
    StatefulTraceClient,
    StateType,
)
from langfuse.extract_model import _extract_model_name
from langfuse.task_manager import TaskManager
from langfuse.utils import _get_timestamp

try:
    from langchain.schema.agent import AgentAction, AgentFinish
    from langchain.schema.document import Document
    from langchain.schema.messages import BaseMessage
    from langchain.schema.output import LLMResult
except ImportError:
    logging.getLogger("langfuse").warning(
        "Could not import langchain. Some functionality may be missing."
    )
    LLMResult = Any
    BaseMessage = Any
    Document = Any
    AgentAction = Any
    AgentFinish = Any


class CallbackHandler(BaseCallbackHandler):
    log = logging.getLogger("langfuse")
    next_span_id: Optional[str] = None
    trace: Optional[StatefulTraceClient]
    root_span: Optional[StatefulSpanClient]
    langfuse: Optional[Langfuse]
    version: Optional[str] = None
    session_id: Optional[str] = None
    user_id: Optional[str] = None
    trace_name: Optional[str] = None
    _task_manager: TaskManager

    def __init__(
        self,
        public_key: Optional[str] = None,
        secret_key: Optional[str] = None,
        host: Optional[str] = None,
        debug: bool = False,
        stateful_client: Optional[
            Union[StatefulTraceClient, StatefulSpanClient]
        ] = None,
        session_id: Optional[str] = None,
        user_id: Optional[str] = None,
        trace_name: Optional[str] = None,
        release: Optional[str] = None,
        version: Optional[str] = None,
        threads: Optional[int] = None,
        flush_at: Optional[int] = None,
        flush_interval: Optional[int] = None,
        max_retries: Optional[int] = None,
        timeout: Optional[int] = None,
    ) -> None:
        # If we're provided a stateful trace client directly
        prioritized_public_key = (
            public_key if public_key else os.environ.get("LANGFUSE_PUBLIC_KEY")
        )
        prioritized_secret_key = (
            secret_key if secret_key else os.environ.get("LANGFUSE_SECRET_KEY")
        )
        prioritized_host = (
            host
            if host
            else os.environ.get("LANGFUSE_HOST", "https://cloud.langfuse.com")
        )

        self.version = version

        if stateful_client and isinstance(stateful_client, StatefulTraceClient):
            self.trace = stateful_client
            self.runs = {}
            self.root_span = None
            self.langfuse = None
            self._task_manager = stateful_client.task_manager

        elif stateful_client and isinstance(stateful_client, StatefulSpanClient):
            self.runs = {}
            self.root_span = stateful_client
            self.langfuse = None
            self.trace = StatefulTraceClient(
                stateful_client.client,
                stateful_client.trace_id,
                StateType.TRACE,
                stateful_client.trace_id,
                stateful_client.task_manager,
            )
            self.runs[stateful_client.id] = stateful_client
            self._task_manager = stateful_client.task_manager

        # Otherwise, initialize stateless using the provided keys
        elif prioritized_public_key and prioritized_secret_key:
            args = {
                "public_key": prioritized_public_key,
                "secret_key": prioritized_secret_key,
                "host": prioritized_host,
                "debug": debug,
            }

            if release is not None:
                args["release"] = release
            if threads is not None:
                args["threads"] = threads
            if flush_at is not None:
                args["flush_at"] = flush_at
            if flush_interval is not None:
                args["flush_interval"] = flush_interval
            if max_retries is not None:
                args["max_retries"] = max_retries
            if timeout is not None:
                args["timeout"] = timeout

            args["sdk_integration"] = "langchain"

            self.langfuse = Langfuse(**args)
            self.trace = None
            self.root_span = None
            self.runs = {}
            self.session_id = session_id
            self.user_id = user_id
            self.trace_name = trace_name
            self._task_manager = self.langfuse.task_manager

        else:
            self.log.error(
                "Either provide a stateful langfuse object or both public_key and secret_key."
            )
            raise ValueError(
                "Either provide a stateful langfuse object or both public_key and secret_key."
            )

    def flush(self):
        if self.trace is not None:
            self.trace.task_manager.flush()
        elif self.root_span is not None:
            self.root_span.task_manager.flush()
        else:
            self.log.debug("There was no trace yet, hence no flushing possible.")

    def auth_check(self):
        if self.langfuse is not None:
            return self.langfuse.auth_check()
        elif self.trace is not None:
            projects = self.trace.client.projects.get()
            if len(projects.data) == 0:
                raise Exception("No projects found for the keys.")
            return True
        elif self.root_span is not None:
            projects = self.root_span.client.projects.get()
            if len(projects) == 0:
                raise Exception("No projects found for the keys.")
            return True

        return False

    def setNextSpan(self, id: str):
        self.next_span_id = id

    def on_llm_new_token(
        self,
        token: str,
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> Any:
        """Run on new LLM token. Only available when streaming is enabled."""
        # Nothing needs to happen here for langfuse. Once the streaming is done,
        self.log.debug(
            f"on llm new token: run_id: {run_id} parent_run_id: {parent_run_id}"
        )

    def get_langchain_run_name(self, serialized: Dict[str, Any], **kwargs: Any) -> str:
        """
        Retrieves the 'run_name' for an entity based on Langchain convention, prioritizing the 'name'
        key in 'kwargs' or falling back to the 'name' or 'id' in 'serialized'. Defaults to "<unknown>"
        if none are available.

        Args:
            serialized (Dict[str, Any]): A dictionary containing the entity's serialized data.
            **kwargs (Any): Additional keyword arguments, potentially including the 'name' override.

        Returns:
            str: The determined Langchain run name for the entity.
        """

        # Check if 'name' is in kwargs and not None, otherwise use default fallback logic
        if "name" in kwargs and kwargs["name"] is not None:
            return kwargs["name"]

        # Fallback to serialized 'name', 'id', or "<unknown>"
        return serialized.get("name", serialized.get("id", ["<unknown>"]))[-1]

    def on_retriever_error(
        self,
        error: Union[Exception, KeyboardInterrupt],
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> Any:
        """Run when Retriever errors."""
        try:
            self.log.debug(
                f"on retriever error: run_id: {run_id} parent_run_id: {parent_run_id}"
            )

            if run_id is None or run_id not in self.runs:
                raise Exception("run not found")

            self.runs[run_id] = self.runs[run_id].end(
                level=ObservationLevel.ERROR,
                status_message=str(error),
                version=self.version,
            )
        except Exception as e:
            self.log.exception(e)

    def on_chain_start(
        self,
        serialized: Dict[str, Any],
        inputs: Dict[str, Any],
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        tags: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> Any:
        try:
            self.log.debug(
                f"on chain start: run_id: {run_id} parent_run_id: {parent_run_id}, name {serialized.get('name', serialized.get('id', ['<unknown>'])[-1])}"
            )
            self.__generate_trace_and_parent(
                serialized=serialized,
                inputs=inputs,
                run_id=run_id,
                parent_run_id=parent_run_id,
                tags=tags,
                metadata=metadata,
                version=self.version,
                **kwargs,
            )

            content = {
                "id": self.next_span_id,
                "trace_id": self.trace.id,
                "name": self.get_langchain_run_name(serialized, **kwargs),
                "metadata": self.__join_tags_and_metadata(tags, metadata),
                "input": inputs,
                "version": self.version,
            }

            if parent_run_id is None:
                if self.root_span is None:
                    self.runs[run_id] = self.trace.span(**content)
                else:
                    self.runs[run_id] = self.root_span.span(**content)
            if parent_run_id is not None:
                self.runs[run_id] = self.runs[parent_run_id].span(**content)

        except Exception as e:
            self.log.exception(e)

    def get_trace_id(self) -> str:
        return self.trace.id

    def get_trace_url(self) -> str:
        return self.trace.get_trace_url()

    def __generate_trace_and_parent(
        self,
        serialized: Dict[str, Any],
        inputs: Dict[str, Any],
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        tags: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ):
        try:
            class_name = self.get_langchain_run_name(serialized, **kwargs)

            # on a new invocation, and not user provided root, we want to initialise a new trace
            # parent_run_id is None when we are at the root of a langchain execution
            if (
                self.trace is not None
                and parent_run_id is None
                and self.langfuse is not None
            ):
                self.trace = None
                self.runs = {}

            # if we are at a root, but langfuse exists, it means we do not have a
            # root provided by a user. Initialise it by creating a trace and root span.
            if self.trace is None and self.langfuse is not None:
                trace = self.langfuse.trace(
                    id=str(run_id),
                    name=self.trace_name if self.trace_name is not None else class_name,
                    metadata=self.__join_tags_and_metadata(tags, metadata),
                    version=self.version,
                    session_id=self.session_id,
                    user_id=self.user_id,
                    input=inputs,
                )

                self.trace = trace

                if parent_run_id is not None and parent_run_id in self.runs:
                    self.runs[run_id] = self.trace.span(
                        id=self.next_span_id,
                        trace_id=self.trace.id,
                        name=class_name,
                        metadata=self.__join_tags_and_metadata(tags, metadata),
                        input=inputs,
                        version=self.version,
                    )

                return

        except Exception as e:
            self.log.exception(e)

    def on_agent_action(
        self,
        action: AgentAction,
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> Any:
        """Run on agent action."""
        try:
            self.log.debug(
                f"on agent action: run_id: {run_id} parent_run_id: {parent_run_id}"
            )

            if run_id not in self.runs:
                raise Exception("run not found")

            self.runs[run_id] = self.runs[run_id].end(
                output=action, version=self.version
            )

        except Exception as e:
            self.log.exception(e)

    def on_agent_finish(
        self,
        finish: AgentFinish,
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> Any:
        try:
            self.log.debug(
                f"on agent finish: run_id: {run_id} parent_run_id: {parent_run_id}"
            )
            if run_id not in self.runs:
                raise Exception("run not found")

            self.runs[run_id] = self.runs[run_id].end(
                output=finish, version=self.version
            )

            self._update_trace(run_id, parent_run_id, finish)

        except Exception as e:
            self.log.exception(e)

    def on_chain_end(
        self,
        outputs: Dict[str, Any],
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> Any:
        try:
            self.log.debug(
                f"on chain end: run_id: {run_id} parent_run_id: {parent_run_id}"
            )

            if run_id not in self.runs:
                raise Exception("run not found")

            self.runs[run_id] = self.runs[run_id].end(
                output=outputs, version=self.version
            )

            self._update_trace(run_id, parent_run_id, outputs)
        except Exception as e:
            self.log.exception(e)

    def on_chain_error(
        self,
        error: Union[Exception, KeyboardInterrupt],
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        tags: Optional[List[str]] = None,
        **kwargs: Any,
    ) -> None:
        try:
            self.log.debug(
                f"on chain error: run_id: {run_id} parent_run_id: {parent_run_id}"
            )
            self.runs[run_id] = self.runs[run_id].end(
                level=ObservationLevel.ERROR,
                status_message=str(error),
                version=self.version,
            )

            self._update_trace(run_id, parent_run_id, error)

        except Exception as e:
            self.log.exception(e)

    def on_chat_model_start(
        self,
        serialized: Dict[str, Any],
        messages: List[List[BaseMessage]],
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        tags: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> Any:
        try:
            self.log.debug(
                f"on chat model start: run_id: {run_id} parent_run_id: {parent_run_id}"
            )
            self.__on_llm_action(
                serialized,
                run_id,
                messages,
                parent_run_id,
                tags=tags,
                metadata=metadata,
                **kwargs,
            )
        except Exception as e:
            self.log.exception(e)

    def on_llm_start(
        self,
        serialized: Dict[str, Any],
        prompts: List[str],
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        tags: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> Any:
        try:
            self.log.debug(
                f"on llm start: run_id: {run_id} parent_run_id: {parent_run_id}"
            )
            self.__on_llm_action(
                serialized,
                run_id,
                prompts,
                parent_run_id,
                tags=tags,
                metadata=metadata,
                **kwargs,
            )
        except Exception as e:
            self.log.exception(e)

    def on_tool_start(
        self,
        serialized: Dict[str, Any],
        input_str: str,
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        tags: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> Any:
        try:
            self.log.debug(
                f"on tool start: run_id: {run_id} parent_run_id: {parent_run_id}"
            )

            if parent_run_id is None or parent_run_id not in self.runs:
                raise Exception("parent run not found")
            meta = self.__join_tags_and_metadata(tags, metadata)

            meta.update(
                {key: value for key, value in kwargs.items() if value is not None}
            )

            self.runs[run_id] = self.runs[parent_run_id].span(
                id=self.next_span_id,
                name=self.get_langchain_run_name(serialized, **kwargs),
                input=input_str,
                metadata=meta,
                version=self.version,
            )
            self.next_span_id = None
        except Exception as e:
            self.log.exception(e)

    def on_retriever_start(
        self,
        serialized: Dict[str, Any],
        query: str,
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        tags: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> Any:
        try:
            self.log.debug(
                f"on retriever start: run_id: {run_id} parent_run_id: {parent_run_id}"
            )

            if parent_run_id is None or parent_run_id not in self.runs:
                raise Exception("parent run not found")

            self.runs[run_id] = self.runs[parent_run_id].span(
                id=self.next_span_id,
                name=self.get_langchain_run_name(serialized, **kwargs),
                input=query,
                metadata=self.__join_tags_and_metadata(tags, metadata),
                version=self.version,
            )
            self.next_span_id = None
        except Exception as e:
            self.log.exception(e)

    def on_retriever_end(
        self,
        documents: Sequence[Document],
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> Any:
        try:
            self.log.debug(
                f"on retriever end: run_id: {run_id} parent_run_id: {parent_run_id}"
            )

            if run_id is None or run_id not in self.runs:
                raise Exception("run not found")

            self.runs[run_id] = self.runs[run_id].end(
                output=documents, version=self.version
            )

            self._update_trace(run_id, parent_run_id, documents)

        except Exception as e:
            self.log.exception(e)

    def on_tool_end(
        self,
        output: str,
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> Any:
        try:
            self.log.debug(
                f"on tool end: run_id: {run_id} parent_run_id: {parent_run_id}"
            )
            if run_id is None or run_id not in self.runs:
                raise Exception("run not found")

            self.runs[run_id] = self.runs[run_id].end(
                output=output, version=self.version
            )

            self._update_trace(run_id, parent_run_id, output)

        except Exception as e:
            self.log.exception(e)

    def on_tool_error(
        self,
        error: Union[Exception, KeyboardInterrupt],
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> Any:
        try:
            self.log.debug(
                f"on tool error: run_id: {run_id} parent_run_id: {parent_run_id}"
            )
            if run_id is None or run_id not in self.runs:
                raise Exception("run not found")

            self.runs[run_id] = self.runs[run_id].end(
                status_message=error, level=ObservationLevel.ERROR, version=self.version
            )

            self._update_trace(run_id, parent_run_id, error)

        except Exception as e:
            self.log.exception(e)

    def __on_llm_action(
        self,
        serialized: Dict[str, Any],
        run_id: UUID,
        prompts: List[str],
        parent_run_id: Optional[UUID] = None,
        tags: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ):
        try:
            self.__generate_trace_and_parent(
                serialized,
                inputs=prompts,
                run_id=run_id,
                parent_run_id=parent_run_id,
                tags=tags,
                metadata=metadata,
                version=self.version,
                kwargs=kwargs,
            )

            model_name = None

            model_name = self._parse_model_and_log_errors(serialized, kwargs)

            content = {
                "name": self.get_langchain_run_name(serialized, **kwargs),
                "input": prompts,
                "metadata": self.__join_tags_and_metadata(tags, metadata),
                "model": model_name,
                "model_parameters": {
                    key: value
                    for key, value in {
                        "temperature": kwargs["invocation_params"].get("temperature"),
                        "max_tokens": kwargs["invocation_params"].get("max_tokens"),
                        "top_p": kwargs["invocation_params"].get("top_p"),
                        "frequency_penalty": kwargs["invocation_params"].get(
                            "frequency_penalty"
                        ),
                        "presence_penalty": kwargs["invocation_params"].get(
                            "presence_penalty"
                        ),
                        "request_timeout": kwargs["invocation_params"].get(
                            "request_timeout"
                        ),
                    }.items()
                    if value is not None
                },
                "version": self.version,
            }

            if parent_run_id in self.runs:
                self.runs[run_id] = self.runs[parent_run_id].generation(**content)
            elif self.root_span is not None and parent_run_id is None:
                self.runs[run_id] = self.root_span.generation(**content)
            else:
                self.runs[run_id] = self.trace.generation(**content)

        except Exception as e:
            self.log.exception(e)

    def _parse_model_and_log_errors(self, serialized, kwargs):
        """Parse the model name from the serialized object or kwargs. If it fails, send the error log to the server and return None."""

        try:
            model_name = _extract_model_name(serialized, **kwargs)
            if model_name:
                return model_name

            if model_name is None:
                self.log.warning(
                    "Langfuse was not able to parse the LLM model. The LLM call will be recorded without model name. Please create an issue so we can fix your integration: https://github.com/langfuse/langfuse/issues/new/choose"
                )
                self._report_error(
                    {
                        "log": "unable to parse model name",
                        "kwargs": str(kwargs),
                        "serialized": str(serialized),
                    }
                )
        except Exception as e:
            self.log.exception(e)
            self.log.warning(
                "Langfuse was not able to parse the LLM model. The LLM call will be recorded without model name. Please create an issue so we can fix your integration: https://github.com/langfuse/langfuse/issues/new/choose"
            )
            self._report_error(
                {
                    "log": "unable to parse model name",
                    "kwargs": str(kwargs),
                    "serialized": str(serialized),
                    "exception": str(e),
                }
            )

            return None

    def on_llm_end(
        self,
        response: LLMResult,
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> Any:
        try:
            self.log.debug(
                f"on llm end: run_id: {run_id} parent_run_id: {parent_run_id} response: {response} kwargs: {kwargs}"
            )
            if run_id not in self.runs:
                raise Exception("Run not found, see docs what to do in this case.")
            else:
                last_response = response.generations[-1][-1]
                llm_usage = (
                    None
                    if response.llm_output is None
                    else response.llm_output["token_usage"]
                )

                extracted_response = _extract_response(last_response)

                self.runs[run_id] = self.runs[run_id].end(
                    output=extracted_response, usage=llm_usage, version=self.version
                )

                self._update_trace(run_id, parent_run_id, extracted_response)

        except Exception as e:
            self.log.exception(e)

    def on_llm_error(
        self,
        error: Union[Exception, KeyboardInterrupt],
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> Any:
        try:
            self.log.debug(
                f"on llm error: run_id: {run_id} parent_run_id: {parent_run_id}"
            )
            self.runs[run_id] = self.runs[run_id].end(
                status_message=str(error),
                level=ObservationLevel.ERROR,
                version=self.version,
            )
            self._update_trace(run_id, parent_run_id, error)

        except Exception as e:
            self.log.exception(e)

    def __join_tags_and_metadata(
        self,
        tags: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        if tags is None and metadata is None:
            return None
        elif tags is not None and len(tags) > 0:
            final_dict = {"tags": tags}
            if metadata is not None:
                final_dict.update(metadata)  # Merge metadata into final_dict
            return final_dict
        else:
            return metadata

    def _report_error(self, error: dict):
        event = SdkLogBody(log=error)

        self._task_manager.add_task(
            {
                "id": str(uuid4()),
                "type": "sdk-log",
                "timestamp": _get_timestamp(),
                "body": event.dict(),
            }
        )

    def _update_trace(self, run_id: str, parent_run_id: Optional[str], output: any):
        """Update the trace with the output of the current run. Called at every finish callback event."""

        if (
            parent_run_id
            is None  # If we are at the root of the langchain execution -> reached the end of the root
            and self.trace is not None  # We do have a trace available
            and self.trace.id
            == str(run_id)  # The trace was generated by langchain and not by the user
        ):
            self.trace = self.trace.update(output=output)


def _extract_response(last_response):
    """Extract the response from the last response of the LLM call."""

    # We return the text of the response if not empty, otherwise the additional_kwargs
    # Additional kwargs contains the response in case of tool usage
    return (
        last_response.text.strip()
        if last_response.text is not None and last_response.text.strip() != ""
        else last_response.message.additional_kwargs
    )
