import logging
from typing import Any

from dhenara.ai.providers.base import AIModelProviderClientBase
from dhenara.ai.providers.shared import APIProviderSharedFns
from dhenara.ai.types.external_api import (
    AIModelAPIProviderEnum,
    FormattedPrompt,
    GoogleAiMessageRoleEnum,
    GoogleAIPromptMessage,
    SystemInstructions,
)
from dhenara.ai.types.genai import AIModel
from dhenara.ai.types.shared.file import FileFormatEnum, GenericFile
from google import genai

logger = logging.getLogger(__name__)


class GoogleAIClientBase(AIModelProviderClientBase):
    """Base class for all Google AI Clients"""

    prompt_message_class = GoogleAIPromptMessage

    async def initialize(self) -> None:
        pass

    async def cleanup(self) -> None:
        pass

    def process_instructions(
        self,
        instructions: SystemInstructions,
    ) -> FormattedPrompt | str | None:
        instructions_str = None
        if instructions:
            if isinstance(instructions, list):
                instructions_str = " ".join(instructions)
            else:
                logger.warning(f"process_instructions: instructions should be a list not {type(instructions)}")
                instructions_str = str(instructions)

            # Some models don't support system instructions
            if any(self.model_endpoint.ai_model.model_name.startswith(model) for model in ["gemini-1.0-pro"]):
                instruction_as_prompt = self.get_prompt(
                    model=self.model_endpoint.ai_model,
                    role=GoogleAiMessageRoleEnum.USER,
                    text=instructions_str,
                    file_contents=[],
                )
                return instruction_as_prompt
        return instructions_str

    async def _setup_client(self) -> genai.Client:
        """Get the appropriate Google AI client based on the provider"""
        api = self.model_endpoint.api

        if api.provider == AIModelAPIProviderEnum.GOOGLE_AI:
            return genai.Client(api_key=api.api_key).aio
        elif api.provider == AIModelAPIProviderEnum.GOOGLE_VERTEX_AI:
            client_params = APIProviderSharedFns.get_vertex_ai_credentials(api)
            return genai.Client(
                vertexai=True,
                credentials=client_params["credentials"],
                project=client_params["project_id"],
                location=client_params["location"],
            ).aio
        else:
            error_msg = f"Unsupported API provider {api.provider} for Google AI"
            logger.error(error_msg)
            raise ValueError(error_msg)

    @staticmethod
    def get_prompt(
        model: AIModel,
        role: GoogleAiMessageRoleEnum,
        text: str,
        file_contents: list,
    ) -> dict:
        # Convert text and file_contents into proper Part format
        parts = []

        # Add the text as a Part
        if text:
            parts.append({"text": text})

        # Add file contents if any
        if file_contents:
            parts.extend(file_contents)

        return {"role": role.value, "parts": parts}

    @staticmethod
    def get_prompt_file_contents(
        model: AIModel,
        files: list[GenericFile],
        max_words: int | None,
    ) -> list[dict[str, Any]]:
        contents = []
        for file in files:
            file_format = file.get_file_format()
            if file_format in [FileFormatEnum.COMPRESSED, FileFormatEnum.TEXT]:
                text = f"\nFile: {file.get_source_file_name()}  Content: {file.get_processed_file_data(max_words)}"
                pcontent = text
                contents.append(
                    {
                        "text": pcontent,
                    }
                )
            elif file_format in [FileFormatEnum.IMAGE]:
                # img_as_text = f"\nImage : {file.get_source_file_name()}  Content: {file.get_processed_file_data_content_only()}"
                mime_type = file.get_mime_type()
                contents.append(
                    {
                        "inline_data": {
                            "data": file.get_processed_file_data_content_only(),  # Bytes type
                            "mime_type": mime_type,
                        },
                    }
                )
            else:
                logger.error(f"get_prompt_file_contents: Unknown file_format {file_format} for file {file.name} ")

        return contents
