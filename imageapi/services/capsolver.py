"""Capsolver reCAPTCHA service for ImageAPI."""

import asyncio
from typing import Optional

import httpx

from ..core.logger import logger

CAPSOLVER_API = "https://api.capsolver.com"


class CapsolverError(Exception):
    pass


class CapsolverService:
    """reCAPTCHA v3 Enterprise solver via Capsolver."""

    def __init__(self, api_key: str):
        self.api_key = api_key

    async def solve_recaptcha_v3(
        self,
        website_url: str,
        website_key: str,
        page_action: str = "IMAGE_GENERATION",
    ) -> str:
        """Solve reCAPTCHA v3 Enterprise and return token."""
        payload = {
            "clientKey": self.api_key,
            "task": {
                "type": "ReCaptchaV3EnterpriseTaskProxyLess",
                "websiteURL": website_url,
                "websiteKey": website_key,
                "pageAction": page_action,
            },
        }

        async with httpx.AsyncClient(timeout=60) as client:
            # Create task
            resp = await client.post(f"{CAPSOLVER_API}/createTask", json=payload)
            data = resp.json()

            if data.get("errorId", 0) != 0:
                raise CapsolverError(f"Create task failed: {data.get('errorDescription', 'Unknown')}")

            task_id = data.get("taskId")
            if not task_id:
                raise CapsolverError("No taskId in response")

            # Poll for result
            for _ in range(30):
                await asyncio.sleep(2)
                resp = await client.post(
                    f"{CAPSOLVER_API}/getTaskResult",
                    json={"clientKey": self.api_key, "taskId": task_id},
                )
                result = resp.json()

                if result.get("errorId", 0) != 0:
                    raise CapsolverError(f"Task error: {result.get('errorDescription')}")

                status = result.get("status")
                if status == "ready":
                    token = result.get("solution", {}).get("gRecaptchaResponse")
                    if not token:
                        raise CapsolverError("No token in solution")
                    logger.debug("reCAPTCHA solved successfully")
                    return token

            raise CapsolverError("Timeout waiting for solution")


def get_capsolver(api_key: str) -> Optional[CapsolverService]:
    if not api_key:
        return None
    return CapsolverService(api_key)
