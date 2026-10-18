"""Capsolver integration for reCAPTCHA solving."""

import asyncio
from typing import Optional

import httpx

from ..core.logger import logger

CAPSOLVER_API_URL = "https://api.capsolver.com"


class CapsolverError(Exception):
    """Capsolver error."""

    pass


class CapsolverService:
    """Service for solving reCAPTCHA using Capsolver API."""

    def __init__(self, api_key: str):
        self.api_key = api_key
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create HTTP client."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=120)
        return self._client

    async def close(self) -> None:
        """Close the HTTP client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def solve_recaptcha_v3(
        self,
        website_url: str,
        website_key: str,
        page_action: str = "verify",
        min_score: float = 0.7,
        max_retries: int = 3,
        proxy: str | None = None,
    ) -> str:
        """
        Solve reCAPTCHA v3 Enterprise.

        Args:
            website_url: The URL of the website with reCAPTCHA
            website_key: The reCAPTCHA site key
            page_action: The action parameter for reCAPTCHA
            min_score: Minimum score required (0.1-0.9)
            max_retries: Maximum number of retry attempts

        Returns:
            The reCAPTCHA token

        Raises:
            CapsolverError: If solving fails
        """
        client = await self._get_client()

        for attempt in range(max_retries):
            try:
                # Create task
                task: dict = {
                    "websiteURL": website_url,
                    "websiteKey": website_key,
                    "pageAction": page_action,
                    "minScore": min_score,
                }
                if proxy:
                    task["type"] = "ReCaptchaV3EnterpriseTask"
                    task["proxy"] = proxy.rstrip("/")
                else:
                    task["type"] = "ReCaptchaV3EnterpriseTaskProxyLess"
                create_response = await client.post(
                    f"{CAPSOLVER_API_URL}/createTask",
                    json={"clientKey": self.api_key, "task": task},
                )
                create_data = create_response.json()

                if create_data.get("errorId", 0) != 0:
                    error_desc = create_data.get(
                        "errorDescription", "Unknown error"
                    )
                    logger.error(f"Capsolver create task error: {error_desc}")
                    if attempt < max_retries - 1:
                        await asyncio.sleep(2)
                        continue
                    raise CapsolverError(f"Create task failed: {error_desc}")

                task_id = create_data.get("taskId")
                if not task_id:
                    raise CapsolverError("No task ID returned")

                logger.debug(f"Capsolver task created: {task_id}")

                # Poll for result
                for _ in range(60):  # Max 2 minutes
                    await asyncio.sleep(2)

                    result_response = await client.post(
                        f"{CAPSOLVER_API_URL}/getTaskResult",
                        json={
                            "clientKey": self.api_key,
                            "taskId": task_id,
                        },
                    )
                    result_data = result_response.json()

                    if result_data.get("errorId", 0) != 0:
                        error_desc = result_data.get(
                            "errorDescription", "Unknown error"
                        )
                        logger.error(
                            f"Capsolver get result error: {error_desc}"
                        )
                        break

                    status = result_data.get("status")

                    if status == "ready":
                        solution = result_data.get("solution", {})
                        token = solution.get("gRecaptchaResponse")
                        if token:
                            logger.info(
                                f"Capsolver solved reCAPTCHA (attempt {attempt + 1})"
                            )
                            return token
                        raise CapsolverError("No token in solution")

                    if status == "failed":
                        logger.error("Capsolver task failed")
                        break

                    # Still processing
                    logger.debug(f"Capsolver status: {status}")

            except httpx.RequestError as e:
                logger.error(f"Capsolver request error: {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(2)
                    continue

            except CapsolverError:
                if attempt < max_retries - 1:
                    await asyncio.sleep(2)
                    continue
                raise

        raise CapsolverError(
            f"Failed to solve reCAPTCHA after {max_retries} attempts"
        )

    async def get_balance(self) -> float:
        """Get Capsolver account balance."""
        client = await self._get_client()

        response = await client.post(
            f"{CAPSOLVER_API_URL}/getBalance",
            json={"clientKey": self.api_key},
        )
        data = response.json()

        if data.get("errorId", 0) != 0:
            raise CapsolverError(
                data.get("errorDescription", "Failed to get balance")
            )

        return data.get("balance", 0)


# Global instance
_capsolver: Optional[CapsolverService] = None


def get_capsolver(api_key: str) -> CapsolverService:
    """Get or create Capsolver service instance."""
    global _capsolver
    if _capsolver is None or _capsolver.api_key != api_key:
        _capsolver = CapsolverService(api_key)
    return _capsolver
