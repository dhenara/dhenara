import logging
from collections.abc import AsyncGenerator

from anthropic.types import (
    ContentBlock,
    Message,
    MessageStreamEvent,
    RawContentBlockDeltaEvent,
    RawContentBlockStartEvent,
    RawContentBlockStopEvent,
    RawMessageDeltaEvent,
    RawMessageStartEvent,
    RawMessageStopEvent,
    RedactedThinkingBlock,
    SignatureDelta,
    TextBlock,
    TextDelta,
    ThinkingBlock,
    ThinkingDelta,
    ToolUseBlock,
)
from dhenara.ai.providers.anthropic import AnthropicClientBase
from dhenara.ai.providers.common import StreamingManager
from dhenara.ai.types.external_api import (
    ExternalApiCallStatus,
)
from dhenara.ai.types.genai import (
    AIModelCallResponse,
    AIModelCallResponseMetaData,
    ChatResponse,
    ChatResponseChoice,
    ChatResponseChoiceDelta,
    ChatResponseContentItem,
    ChatResponseContentItemDelta,
    ChatResponseReasoningContentItem,
    ChatResponseReasoningContentItemDelta,
    ChatResponseTextContentItem,
    ChatResponseTextContentItemDelta,
    ChatResponseToolCallContentItem,
    ChatResponseUsage,
    StreamingChatResponse,
)
from dhenara.ai.types.shared.api import SSEErrorResponse

logger = logging.getLogger(__name__)


# -----------------------------------------------------------------------------
class AnthropicChat(AnthropicClientBase):
    async def generate_response(
        self,
        prompt: dict,
        context: list[dict] | None = None,
        instructions: list[str] | None = None,
    ) -> AIModelCallResponse:
        if not self._client:
            raise RuntimeError("Client not initialized. Use with 'async with' context manager")

        if self._input_validation_pending:
            raise ValueError("inputs must be validated with validate_inputs() before api calls")

        parsed_response: ChatResponse | None = None
        api_call_status: ExternalApiCallStatus | None = None

        messages = []
        user = self.config.get_user()

        # Process system instructions
        system_prompt = self.process_instructions(instructions)

        # Add previous messages and current prompt
        if context:
            messages.extend(context)
        messages.append(prompt)

        # Prepare API call arguments
        chat_args = {
            "model": self.model_name_in_api_calls,
            "messages": messages,
            "stream": self.config.streaming,
        }

        if system_prompt:
            chat_args["system"] = system_prompt

        if user:
            chat_args["metadata"] = {"user_id": user}

        max_output_tokens, max_reasoning_tokens = self.config.get_max_output_tokens(self.model_endpoint.ai_model)
        if max_output_tokens is not None:
            chat_args["max_tokens"] = max_output_tokens

        if max_reasoning_tokens is None:
            chat_args["thinking"] = {
                "type": "disabled",
            }
        else:
            chat_args["thinking"] = {
                "type": "enabled",
                "budget_tokens": max_reasoning_tokens,
            }

        if self.config.options:
            chat_args.update(self.config.options)

        if self.config.test_mode:
            from dhenara.ai.providers.common.dummy import DummyAIModelResponseFns

            return await DummyAIModelResponseFns.get_dummy_ai_model_response(
                ai_model_ep=self.model_endpoint,
                streaming=self.config.streaming,
            )

        try:
            if self.config.streaming:
                stream = await self._client.messages.create(**chat_args)
                stream_generator = self._handle_streaming_response(stream=stream)
                return AIModelCallResponse(stream_generator=stream_generator)

            response = await self._client.messages.create(**chat_args)
            parsed_response = self.parse_response(response)
            api_call_status = self._create_success_status()

        except Exception as e:
            logger.exception(f"Error in generate_response: {e}")
            api_call_status = self._create_error_status(str(e))

        return AIModelCallResponse(
            status=api_call_status,
            chat_response=parsed_response,
        )

    async def _handle_streaming_response(
        self,
        stream: AsyncGenerator[MessageStreamEvent],
    ) -> AsyncGenerator[tuple[StreamingChatResponse | SSEErrorResponse, AIModelCallResponse | None]]:
        """Handle streaming response with progress tracking and final response"""
        stream_manager = StreamingManager(
            model_endpoint=self.model_endpoint,
        )

        # message_metadata is used to preserve params of initial message across chunks
        message_metadata = {}

        try:
            async for chunk in stream:
                stream_response: StreamingChatResponse | None = None

                if isinstance(chunk, RawMessageStartEvent):
                    message = chunk.message

                    # Initialize message metadata
                    message_metadata = {
                        "id": message.id,
                        "model": message.model,
                        "role": message.role,
                        "type": type,
                        "index": 0,  # Only one choice from Antropic
                    }

                    # Anthropic has a wieded way of reporint usage on streaming
                    # On message_start, usage will have input tokens and few output tokens
                    _usage = chunk.message.usage
                    if _usage:
                        # Initialize usage in stream_manager
                        usage = ChatResponseUsage(
                            total_tokens=0,
                            prompt_tokens=_usage.input_tokens,
                            completion_tokens=_usage.output_tokens,
                        )
                        stream_manager.update_usage(usage)

                elif isinstance(chunk, RawContentBlockStartEvent):
                    block_type = chunk.content_block.type
                    if block_type == "redacted_thinking":
                        content_deltas = [
                            ChatResponseReasoningContentItem(
                                index=chunk.index,
                                role=message_metadata["role"],
                                metadata={
                                    "redacted_thinking_data": chunk.content_block.data,
                                },
                            )
                        ]

                        choice_deltas = [
                            ChatResponseChoiceDelta(
                                index=message_metadata["index"],
                                content_deltas=content_deltas,
                                metadata=None,
                            )
                        ]

                        response_chunk = stream_manager.update(choice_deltas=choice_deltas)
                        stream_response = StreamingChatResponse(
                            id=message_metadata["id"],
                            data=response_chunk,
                        )

                        yield stream_response, None
                    elif block_type in ["text", "thinking"]:
                        pass
                    else:
                        logger.debug(f"anthropic: Unhandled content_block_type {block_type}")

                elif isinstance(chunk, RawContentBlockDeltaEvent):
                    content_deltas = [
                        self.process_content_item_delta(
                            index=chunk.index,
                            role=message_metadata["role"],
                            delta=chunk.delta,
                        )
                    ]

                    choice_deltas = [
                        ChatResponseChoiceDelta(
                            index=message_metadata["index"],
                            content_deltas=content_deltas,
                            metadata=None,
                        )
                    ]
                    response_chunk = stream_manager.update(choice_deltas=choice_deltas)
                    stream_response = StreamingChatResponse(
                        id=message_metadata["id"],
                        data=response_chunk,
                    )
                    yield stream_response, None
                elif isinstance(chunk, RawContentBlockStopEvent):
                    pass
                elif isinstance(chunk, RawMessageDeltaEvent):
                    # Update output tokens
                    stream_manager.usage.completion_tokens += chunk.usage.output_tokens
                    stream_manager.usage.total_tokens = stream_manager.usage.prompt_tokens + stream_manager.usage.completion_tokens

                    # Update choice metatdata
                    choice_deltas = [
                        ChatResponseChoiceDelta(
                            index=message_metadata["index"],
                            finish_reason=chunk.delta.stop_reason,
                            stop_sequence=chunk.delta.stop_sequence,
                            content_deltas=[],
                            metadata={},
                        )
                    ]
                    response_chunk = stream_manager.update(choice_deltas=choice_deltas)
                    stream_response = StreamingChatResponse(
                        id=message_metadata["id"],
                        data=response_chunk,
                    )
                    yield stream_response, None

                elif isinstance(chunk, RawMessageStopEvent):
                    pass
                else:
                    logger.debug(f"anthropic: Unhandled message type {chunk.type}")

            # API has stopped streaming, get final response
            logger.debug("API has stopped streaming, processsing final response")
            final_response = stream_manager.complete()

            yield None, final_response
            return  # Stop the generator

        except Exception as e:
            error_response = self._create_streaming_error_response(exc=e)
            yield error_response, None

    def _get_usage_from_provider_response(
        self,
        response: Message,
    ) -> ChatResponseUsage:
        return ChatResponseUsage(
            total_tokens=response.usage.input_tokens + response.usage.output_tokens,
            prompt_tokens=response.usage.input_tokens,
            completion_tokens=response.usage.output_tokens,
        )

    def parse_response(self, response: Message) -> ChatResponse:
        usage, usage_charge = self.get_usage_and_charge(response)

        return ChatResponse(
            model=response.model,
            provider=self.model_endpoint.ai_model.provider,
            api_provider=self.model_endpoint.api.provider,
            usage=usage,
            usage_charge=usage_charge,
            choices=[
                ChatResponseChoice(
                    index=0,  # Only one choice
                    finish_reason=response.stop_reason,
                    stop_sequence=response.stop_sequence,
                    contents=[
                        self.process_content_item(
                            index=content_index,  # For non-streaming response, Anthropic APIs doesn't provide index, so enumerate
                            role=response.role,
                            content_item=content_item,
                        )
                        for content_index, content_item in enumerate(response.content)
                    ],
                    metadata={},  # Choice metadata
                ),
            ],
            # Response Metadata
            metadata=AIModelCallResponseMetaData(
                streaming=False,
                duration_seconds=0,  # TODO
                provider_metadata={
                    "id": response.id,
                },
            ),
        )

    def process_content_item(
        self,
        index: int,
        role: str,
        content_item: ContentBlock,
    ) -> ChatResponseContentItem:
        if isinstance(content_item, TextBlock):
            return ChatResponseTextContentItem(
                index=index,
                role=role,
                text=content_item.text,
            )
        elif isinstance(content_item, ThinkingBlock):
            return ChatResponseReasoningContentItem(
                index=index,
                role=role,
                thinking_text=content_item.thinking,
                metadata={
                    "signature": content_item.signature,
                },
            )

        elif isinstance(content_item, RedactedThinkingBlock):
            return ChatResponseReasoningContentItem(
                index=index,
                role=role,
                metadata={
                    "redacted_thinking_data": content_item.data,
                },
            )

        elif isinstance(content_item, ToolUseBlock):
            return ChatResponseToolCallContentItem(
                index=index,
                role=role,
                metadata={
                    "tool_use": {
                        "id": content_item.id,
                        "input": content_item.input,
                        "name": content_item.name,
                    }
                },
            )
        else:
            return self.get_unknown_content_type_item(role=role, unknown_item=content_item, streaming=False)

    def process_content_item_delta(
        self,
        index: int,
        role: str,
        delta,
    ) -> ChatResponseContentItemDelta:
        if isinstance(delta, TextDelta):
            return ChatResponseTextContentItemDelta(
                index=index,
                role=role,
                text_delta=delta.text,
            )
        elif isinstance(delta, ThinkingDelta):
            return ChatResponseReasoningContentItemDelta(
                index=index,
                role=role,
                thinking_text_delta=delta.thinking,
                metadata={},
            )
        elif isinstance(delta, SignatureDelta):
            return ChatResponseReasoningContentItemDelta(
                index=index,
                role=role,
                thinking_text_delta="",
                metadata={
                    "signature": delta.signature,
                },
            )

        else:
            return self.get_unknown_content_type_item(role=role, unknown_item=delta, streaming=True)
