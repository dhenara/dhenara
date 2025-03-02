import logging
from collections.abc import AsyncGenerator, Generator
from typing import Union

from dhenara.ai.types.external_api import ExternalApiCallStatus
from dhenara.ai.types.genai.dhenara import (
    ChatResponse,
    ImageResponse,
    StreamingChatResponse,
)
from dhenara.ai.types.shared.base import BaseModel
from pydantic import Field

logger = logging.getLogger(__name__)


class AIModelCallResponse(BaseModel):
    """
    Response model for AI model calls including both streaming and non-streaming responses.

    Attributes:
        status: Current status of the API call
        chat_response: Response for non-streaming chat API calls
        stream_generator: Async generator for streaming chat responses
        image_response: Response for image generation API calls
    """

    status: ExternalApiCallStatus = Field(
        default=None,
        description="API Call status",
    )
    chat_response: ChatResponse | None = Field(
        default=None,
        description="Response for Non-streaming chat creation API calls",
    )
    async_stream_generator: AsyncGenerator[tuple[StreamingChatResponse, Union["AIModelCallResponse", None]], None] | None = Field(
        default=None,
        description="""Response for streaming chat creation API calls.
        This will be an async generator that generates the response stream, and on the last chunk
        along with the full response on the last chunk""",
    )
    sync_stream_generator: Generator[tuple[StreamingChatResponse, Union["AIModelCallResponse", None]], None] | None = Field(
        default=None,
        description="""Sync response for streaming chat creation API calls.
        This will be an async generator that generates the response stream, and on the last chunk
        along with the full response on the last chunk""",
    )
    image_response: ImageResponse | None = Field(
        default=None,
        description="Response for Non-streaming chat creation API calls",
    )

    @property
    def full_response(self) -> ChatResponse | ImageResponse | None:
        """
        Get the full response from either chat or image response.

        Returns:
            ChatResponse | ImageResponse | None: The complete response object
        """
        return self.chat_response if self.chat_response else self.image_response

    @property
    def stream_generator(self) -> AsyncGenerator | Generator | None:
        return self.async_stream_generator if self.async_stream_generator else self.sync_stream_generator
