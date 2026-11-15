import asyncio
import hashlib
import uuid
from typing import Optional, Union

from fastapi import status
from fastapi.responses import JSONResponse, PlainTextResponse

from ...agents.interaction_agent.runtime import InteractionAgentRuntime, track_interaction
from ...logging_config import logger
from ...models import ChatMessage, ChatRequest
from ...utils import error_response


# Extract the most recent user message from the chat request payload
def _extract_latest_user_message(payload: ChatRequest) -> Optional[ChatMessage]:
    for message in reversed(payload.messages):
        if message.role.lower().strip() == "user" and message.content.strip():
            return message
    return None


# Process incoming chat requests by routing them to the interaction agent runtime
async def handle_chat_request(payload: ChatRequest, idempotency_key: Optional[str] = None) -> Union[PlainTextResponse, JSONResponse]:
    """Handle a chat request using the InteractionAgentRuntime."""

    # Extract user message
    user_message = _extract_latest_user_message(payload)
    if user_message is None:
        return error_response("Missing user message", status_code=status.HTTP_400_BAD_REQUEST)

    user_content = user_message.content.strip()  # Already checked in _extract_latest_user_message
    if idempotency_key is not None and (not idempotency_key.strip() or len(idempotency_key) > 200):
        return error_response("Idempotency-Key must contain 1 to 200 characters", status_code=status.HTTP_400_BAD_REQUEST)
    request_id = ("user:" + hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
                  if idempotency_key else "user:" + uuid.uuid4().hex)

    logger.info("chat request", extra={"message_length": len(user_content)})

    try:
        runtime = InteractionAgentRuntime()
        turn = runtime.register_turn(user_content, request_id)
    except ValueError as ve:
        # Missing API key error
        logger.error("configuration error", extra={"error": str(ve)})
        conflict = "Idempotency key" in str(ve)
        paused = "admission is paused" in str(ve)
        return error_response(str(ve), status_code=status.HTTP_503_SERVICE_UNAVAILABLE if paused else
                              status.HTTP_409_CONFLICT if conflict else status.HTTP_400_BAD_REQUEST)

    async def _run_interaction() -> None:
        try:
            await runtime.execute(user_message=user_content, request_id=request_id)
        except Exception as exc:  # pragma: no cover - defensive
            logger.error("chat task failed", extra={"error": str(exc)})

    if turn["status"] != "completed":
        track_interaction(asyncio.create_task(_run_interaction(), name=request_id))

    return PlainTextResponse("", status_code=status.HTTP_202_ACCEPTED, headers={"X-Request-ID": request_id})
