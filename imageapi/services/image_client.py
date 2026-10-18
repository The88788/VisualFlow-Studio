"""Google Image Generation API client (Google Labs Flow / PINHOLE)."""

import asyncio
import base64
import json
import random
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from curl_cffi.requests import AsyncSession

from ..core.logger import logger
from .capsolver import CapsolverService, CapsolverError

# Constants
BASE_URL = "https://aisandbox-pa.googleapis.com"
WEBSITE_URL = "https://labs.google"
RECAPTCHA_KEY = "6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV"

# Model mapping: API name → Google internal name
MODEL_MAPPING = {
    "fast": "GEM_PIX_2",
    "thinking": "NARWHAL",
}

# Aspect ratio mapping: user-friendly → Google enum
ASPECT_RATIO_MAPPING = {
    "1:1": "IMAGE_ASPECT_RATIO_SQUARE",
    "16:9": "IMAGE_ASPECT_RATIO_LANDSCAPE",
    "9:16": "IMAGE_ASPECT_RATIO_PORTRAIT",
    "4:3": "IMAGE_ASPECT_RATIO_LANDSCAPE_FOUR_THREE",
    "3:4": "IMAGE_ASPECT_RATIO_PORTRAIT_THREE_FOUR",
    "3:2": "IMAGE_ASPECT_RATIO_3_2",
    "2:3": "IMAGE_ASPECT_RATIO_2_3",
    "21:9": "IMAGE_ASPECT_RATIO_21_9",
    "4:5": "IMAGE_ASPECT_RATIO_4_5",
    "5:4": "IMAGE_ASPECT_RATIO_5_4",
}


@dataclass
class TokenInfo:
    """Token exchange result."""
    at: str
    expires_at: float
    project_id: str
    credits: Optional[int] = None
    user_paygate_tier: Optional[str] = None


@dataclass
class GeneratedImage:
    """Single generated image."""
    data: bytes  # raw PNG bytes
    seed: Optional[int] = None
    mime_type: str = "image/png"
    media_id: Optional[str] = None


@dataclass
class ImageGenerationResult:
    """Result from image generation."""
    images: list[GeneratedImage] = field(default_factory=list)
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    filtered: bool = False


class ImageClientError(Exception):
    """Image client error."""

    def __init__(self, message: str, error_code: Optional[str] = None, retry: bool = False):
        super().__init__(message)
        self.error_code = error_code
        self.retry = retry


class ImageClient:
    """Google Image Generation API client using curl_cffi."""

    def __init__(
        self,
        capsolver: Optional[CapsolverService] = None,
        proxy: Optional[str] = None,
    ):
        self.capsolver = capsolver
        self.proxy = proxy
        self._session: Optional[AsyncSession] = None

    async def _get_session(self) -> AsyncSession:
        if self._session is None:
            self._session = AsyncSession(
                impersonate="chrome",
                proxy=self.proxy,
                timeout=120,  # 120s (curl_cffi default 30s too low for Google API)
            )
        return self._session

    async def close(self) -> None:
        if self._session:
            await self._session.close()
            self._session = None

    async def _request_with_retry(self, method: str, url: str, max_retries: int = 3, **kwargs):
        """Make request with exponential backoff."""
        session = await self._get_session()
        delay = 1
        last_error = None

        for attempt in range(max_retries):
            try:
                if method.upper() == "POST":
                    response = await session.post(url, **kwargs)
                elif method.upper() == "GET":
                    response = await session.get(url, **kwargs)
                else:
                    response = await session.request(method, url, **kwargs)

                if response.status_code < 500 and response.status_code != 429:
                    return response

                if response.status_code == 429:
                    logger.warning(f"Rate limited, attempt {attempt + 1}/{max_retries}")
                else:
                    logger.warning(f"Server error {response.status_code}, attempt {attempt + 1}/{max_retries}")

            except Exception as e:
                logger.warning(f"Request error: {e}, attempt {attempt + 1}/{max_retries}")
                last_error = e

            if attempt < max_retries - 1:
                await asyncio.sleep(delay)
                delay *= 2

        if last_error:
            raise last_error
        return response

    def _get_headers(self, at: Optional[str] = None) -> dict[str, str]:
        headers = {
            "accept": "*/*",
            "accept-language": "en-US,en;q=0.9",
            "content-type": "application/json",
            "origin": "https://labs.google",
            "referer": "https://labs.google/",
            "user-agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            "x-client-data": "CKu1yQEIirbJAQiktskBCKmdygEIn5nLAQid68sBCJShzQEI5fLNAQiX9M0BCMv2zQEI2ffNAQj5+c0BCLf7zQEI3/3NAQih/s0BCL3+zQEI8oTOAQi8hc4BCPyFzgEIi4bOAQ==",
        }
        if at:
            headers["authorization"] = f"Bearer {at}"
        return headers

    async def exchange_st_for_at(self, st: str) -> TokenInfo:
        """Exchange session token for access token (same as veoapi2)."""
        session = await self._get_session()
        session.cookies.clear()

        headers = {
            "accept": "*/*",
            "accept-language": "en-US,en;q=0.9",
            "cookie": f"__Secure-next-auth.session-token={st}",
            "user-agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
        }

        try:
            url = "https://labs.google/fx/api/auth/session"
            response = await session.get(url, headers=headers)

            if response.status_code == 401:
                raise ImageClientError("Invalid session token", error_code="UNAUTHORIZED")
            if response.status_code == 403:
                raise ImageClientError("Access forbidden", error_code="FORBIDDEN", retry=True)
            if response.status_code == 429:
                raise ImageClientError("Rate limited", error_code="RATE_LIMITED")
            if response.status_code != 200:
                raise ImageClientError(
                    f"Token exchange failed: {response.status_code}",
                    error_code="TOKEN_EXCHANGE_FAILED",
                    retry=True,
                )

            data = response.json()
            at = data.get("access_token") or data.get("accessToken")
            if not at:
                raise ImageClientError("No access token in response", error_code="NO_ACCESS_TOKEN")

            expires_str = data.get("expires")
            expires_at = time.time() + 3600
            if expires_str:
                try:
                    from datetime import datetime
                    dt = datetime.fromisoformat(expires_str.replace("Z", "+00:00"))
                    expires_at = dt.timestamp()
                except Exception:
                    pass

            # Fetch credits and tier (like veoapi2)
            credits_data = await self.get_credits(at)
            credits = credits_data.get("credits")
            user_paygate_tier = credits_data.get("userPaygateTier", "PAYGATE_TIER_ONE")

            logger.info(
                f"Token exchanged successfully (tier={user_paygate_tier}, credits={credits})"
            )

            return TokenInfo(
                at=at,
                expires_at=expires_at,
                project_id="",
                credits=credits,
                user_paygate_tier=user_paygate_tier,
            )

        except ImageClientError:
            raise
        except Exception as e:
            raise ImageClientError(f"Token exchange error: {e}", error_code="TOKEN_EXCHANGE_ERROR", retry=True)

    async def get_credits(self, at: str) -> dict:
        """Get credits and paygate tier."""
        session = await self._get_session()
        headers = self._get_headers(at)
        headers["content-type"] = "application/json"

        try:
            response = await session.get(f"{BASE_URL}/v1/credits", headers=headers)
            if response.status_code == 401:
                raise ImageClientError("Token unauthorized", error_code="UNAUTHORIZED")
            if response.status_code != 200:
                return {"credits": None, "userPaygateTier": "PAYGATE_TIER_ONE"}

            data = response.json()
            return {
                "credits": data.get("credits"),
                "userPaygateTier": data.get("userPaygateTier", "PAYGATE_TIER_ONE"),
            }
        except ImageClientError:
            raise
        except Exception as e:
            logger.warning(f"Failed to get credits: {e}")
            return {"credits": None, "userPaygateTier": "PAYGATE_TIER_ONE"}

    async def create_project(self, st: str, title: str = "Image Project") -> Optional[str]:
        """Create a new project via TRPC (same as veoapi2)."""
        session = await self._get_session()
        headers = self._get_headers()
        headers["cookie"] = f"__Secure-next-auth.session-token={st}"

        payload = {
            "json": {
                "projectTitle": title,
                "toolName": "PINHOLE",
            }
        }

        try:
            url = "https://labs.google/fx/api/trpc/project.createProject"
            response = await session.post(url, headers=headers, json=payload)

            if response.status_code != 200:
                logger.error(f"Create project failed: {response.status_code} {response.text[:500]}")
                return None

            data = response.json()
            # TRPC nested response: result -> data -> json -> result -> projectId
            project_id = (
                data.get("result", {})
                .get("data", {})
                .get("json", {})
                .get("result", {})
                .get("projectId")
            )
            if project_id:
                logger.info(f"Created project: {project_id}")
                return project_id

            logger.error(f"No projectId in response: {json.dumps(data)[:500]}")
            return None
        except Exception as e:
            logger.error(f"Failed to create project: {e}")
            return None

    @staticmethod
    def _detect_image_mime_type(image_bytes: bytes) -> str:
        """Detect image MIME type from magic bytes."""
        if len(image_bytes) < 12:
            return "image/jpeg"
        if image_bytes[:4] == b'RIFF' and image_bytes[8:12] == b'WEBP':
            return "image/webp"
        if image_bytes[:4] == b'\x89PNG':
            return "image/png"
        if image_bytes[:3] == b'\xff\xd8\xff':
            return "image/jpeg"
        if image_bytes[:6] in (b'GIF87a', b'GIF89a'):
            return "image/gif"
        if image_bytes[:2] == b'BM':
            return "image/bmp"
        return "image/jpeg"

    async def upload_image(
        self,
        at: str,
        image_data: bytes,
        project_id: str,
        file_name: Optional[str] = None,
    ) -> tuple[str, str]:
        """
        Upload a reference image via /v1/flow/uploadImage (PINHOLE endpoint).

        Args:
            at: Access token
            image_data: Raw image bytes
            project_id: Google project ID (required for upload)
            file_name: Optional filename hint

        Returns:
            (media_name, workflow_id) — media_name is UUID for imageInputs,
            workflow_id is UUID for batchDeleteAssets cleanup
        """
        session = await self._get_session()
        headers = self._get_headers(at)
        headers["content-type"] = "text/plain;charset=UTF-8"

        url = f"{BASE_URL}/v1/flow/uploadImage"

        mime_type = self._detect_image_mime_type(image_data)
        image_base64 = base64.b64encode(image_data).decode("utf-8")

        # Pick extension from mime
        ext_map = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif"}
        ext = ext_map.get(mime_type, "jpg")
        fname = file_name or f"ref_{uuid.uuid4().hex[:8]}.{ext}"

        payload = {
            "clientContext": {
                "projectId": project_id,
                "tool": "PINHOLE",
            },
            "imageBytes": image_base64,
            "isUserUploaded": True,
            "isHidden": False,
            "mimeType": mime_type,
            "fileName": fname,
        }

        try:
            payload_str = json.dumps(payload, separators=(",", ":"))
            response = await session.post(url, headers=headers, data=payload_str)

            if response.status_code != 200:
                resp_text = response.text[:500] if response.text else ""
                error_code = "IMAGE_UPLOAD_FAILED"
                error_message = ""
                try:
                    err_data = response.json()
                    found = self._find_public_error(err_data)
                    if found:
                        error_code = found
                    elif isinstance(err_data, dict):
                        err_obj = err_data.get("error", err_data)
                        if isinstance(err_obj, dict):
                            for fld in ("status", "failureReason", "code"):
                                val = str(err_obj.get(fld, ""))
                                if val and val not in ("0", ""):
                                    error_code = val
                                    break
                            error_message = str(err_obj.get("message", ""))
                except Exception:
                    pass
                raise ImageClientError(
                    error_message or f"Image upload failed: {response.status_code} {resp_text[:200]}",
                    error_code=error_code,
                    retry=not error_code.startswith("PUBLIC_ERROR"),
                )

            data = response.json()
            # Response: {"media": {"name": "uuid", ...}, "workflow": {"name": "uuid", ...}}
            media_name = data.get("media", {}).get("name")
            workflow_id = data.get("workflow", {}).get("name")

            if not media_name:
                raise ImageClientError(
                    f"No media.name in upload response: {json.dumps(data, default=str)[:500]}",
                    error_code="NO_MEDIA_ID",
                )

            logger.info(f"Image uploaded: media={media_name}, workflow={workflow_id}")
            return media_name, workflow_id or ""

        except ImageClientError:
            raise
        except Exception as e:
            logger.error(f"Image upload error: {e}")
            raise ImageClientError(f"Image upload failed: {e}", error_code="IMAGE_UPLOAD_ERROR", retry=True)

    async def delete_assets(
        self,
        at: str,
        project_id: str,
        workflow_ids: list[str],
    ) -> None:
        """Delete uploaded assets via /v1/flow:batchDeleteAssets."""
        if not workflow_ids:
            return

        session = await self._get_session()
        headers = self._get_headers(at)
        headers["content-type"] = "text/plain;charset=UTF-8"

        payload = {
            "projectId": project_id,
            "workflowIds": workflow_ids,
        }

        try:
            url = f"{BASE_URL}/v1/flow:batchDeleteAssets"
            payload_str = json.dumps(payload, separators=(",", ":"))
            response = await session.post(url, headers=headers, data=payload_str)
            if response.status_code == 200:
                logger.debug(f"Deleted {len(workflow_ids)} uploaded asset(s) from project {project_id}")
            else:
                logger.warning(f"batchDeleteAssets returned {response.status_code}: {response.text[:200]}")
        except Exception as e:
            logger.warning(f"Failed to delete assets: {e}")

    async def generate_images(
        self,
        at: str,
        st: str,
        prompt: str,
        model: str = "fast",
        aspect_ratio: str = "1:1",
        project_id: Optional[str] = None,
        seed: Optional[int] = None,
        image_inputs: Optional[list[dict]] = None,
        user_paygate_tier: Optional[str] = None,
    ) -> ImageGenerationResult:
        """
        Generate images via Google Labs Flow batchGenerateImages.

        Args:
            at: Access token
            st: Session token (for project creation if needed)
            prompt: Text prompt
            model: "fast" or "thinking"
            aspect_ratio: "1:1", "16:9", "9:16", etc.
            project_id: Google project ID
            seed: Random seed
            image_inputs: Optional reference image inputs

        Returns:
            ImageGenerationResult with generated images
        """
        # Ensure project ID
        if not project_id:
            project_id = await self.create_project(st)
            if not project_id:
                raise ImageClientError(
                    "Failed to create project",
                    error_code="PROJECT_CREATION_FAILED",
                    retry=True,
                )

        session = await self._get_session()
        headers = self._get_headers(at)

        # Map model and aspect ratio
        google_model = MODEL_MAPPING.get(model, "GEM_PIX_2")
        google_aspect = ASPECT_RATIO_MAPPING.get(aspect_ratio, "IMAGE_ASPECT_RATIO_SQUARE")
        actual_seed = seed if seed is not None else random.randint(1, 999999)

        # Session and batch IDs
        session_id = f";{int(time.time() * 1000)}"
        batch_id = str(uuid.uuid4())

        # Solve reCAPTCHA
        recaptcha_token = ""
        if self.capsolver:
            try:
                recaptcha_token = await self.capsolver.solve_recaptcha_v3(
                    website_url=f"{WEBSITE_URL}/fx/tools/flow/project/{project_id}",
                    website_key=RECAPTCHA_KEY,
                    page_action="IMAGE_GENERATION",
                )
            except CapsolverError as e:
                logger.error(f"Failed to solve reCAPTCHA: {e}")
                raise ImageClientError(str(e), error_code="CAPTCHA_FAILED", retry=True)

        # Build client context
        client_context = {
            "projectId": project_id,
            "tool": "PINHOLE",
            "sessionId": session_id,
            "userPaygateTier": user_paygate_tier or "PAYGATE_TIER_ONE",
        }
        if recaptcha_token:
            client_context["recaptchaContext"] = {
                "token": recaptcha_token,
                "applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB",
            }

        # Build request
        request_obj = {
            "clientContext": client_context,
            "imageModelName": google_model,
            "imageAspectRatio": google_aspect,
            "structuredPrompt": {
                "parts": [{"text": prompt}],
            },
            "seed": actual_seed,
            "imageInputs": image_inputs or [],
        }

        payload = {
            "clientContext": client_context,
            "mediaGenerationContext": {
                "batchId": batch_id,
            },
            "useNewMedia": True,
            "requests": [request_obj],
        }

        try:
            url = f"{BASE_URL}/v1/projects/{project_id}/flowMedia:batchGenerateImages"
            headers["content-type"] = "text/plain;charset=UTF-8"
            payload_str = json.dumps(payload, separators=(",", ":"))

            logger.info(f"Generating images: model={google_model}, aspect={google_aspect}, seed={actual_seed}")
            logger.debug(f"Payload: {payload_str[:500]}")

            response = await session.post(url, headers=headers, data=payload_str)

            if response.status_code == 401:
                raise ImageClientError("Unauthorized", error_code="UNAUTHORIZED", retry=True)
            if response.status_code == 403:
                raise ImageClientError("Forbidden (reCAPTCHA or permission)", error_code="FORBIDDEN", retry=True)
            if response.status_code == 429:
                raise ImageClientError("Rate limited", error_code="RATE_LIMITED")

            if response.status_code != 200:
                resp_text = response.text[:500] if response.text else ""
                error_code = "GENERATION_FAILED"
                error_message = ""

                try:
                    err_data = response.json()
                    found = self._find_public_error(err_data)
                    if found:
                        error_code = found
                    elif isinstance(err_data, dict):
                        err_obj = err_data.get("error", err_data)
                        if isinstance(err_obj, dict):
                            for fld in ("status", "failureReason", "code"):
                                val = str(err_obj.get(fld, ""))
                                if val and val not in ("0", ""):
                                    error_code = val
                                    break
                            error_message = str(err_obj.get("message", ""))
                except Exception:
                    pass

                is_filtered = "FILTERED" in error_code.upper() or "SAFETY" in error_code.upper()
                if is_filtered:
                    return ImageGenerationResult(filtered=True, error_code=error_code, error_message=error_message or resp_text)

                raise ImageClientError(
                    error_message or f"Generation failed: {response.status_code} {resp_text[:300]}",
                    error_code=error_code,
                    retry=not error_code.startswith("PUBLIC_ERROR"),
                )

            # Parse response — may need to download images from fifeUrl
            data = response.json()
            return await self._parse_image_response(data, session, headers)

        except ImageClientError:
            raise
        except Exception as e:
            logger.error(f"Generate images error: {e}")
            raise ImageClientError(f"Generation failed: {e}", error_code="GENERATION_ERROR", retry=True)

    async def _parse_image_response(self, data: dict, session=None, headers=None) -> ImageGenerationResult:
        """Parse batchGenerateImages response into ImageGenerationResult."""
        result = ImageGenerationResult()

        try:
            # Format A: {media: [{image: {generatedImage: {fifeUrl, seed, ...}}}]}
            media_list = data.get("media") or []
            for media_item in media_list:
                if not isinstance(media_item, dict):
                    continue
                media_id = media_item.get("name")
                image_obj = media_item.get("image", {})
                gen_image = image_obj.get("generatedImage", {})

                fife_url = gen_image.get("fifeUrl", "")
                seed_val = gen_image.get("seed")

                # Try base64 first
                image_bytes_b64 = (
                    gen_image.get("imageBytes")
                    or gen_image.get("encodedImage")
                    or image_obj.get("imageBytes")
                    or image_obj.get("encodedImage")
                )
                if image_bytes_b64:
                    raw = base64.b64decode(image_bytes_b64)
                    result.images.append(GeneratedImage(data=raw, seed=seed_val, mime_type="image/png", media_id=media_id))
                    continue

                # Download from fifeUrl
                if fife_url and session:
                    try:
                        dl_headers = {
                            "accept": "image/*,*/*",
                            "user-agent": headers.get("user-agent", "") if headers else "",
                        }
                        dl_resp = await session.get(fife_url, headers=dl_headers)
                        if dl_resp.status_code == 200 and len(dl_resp.content) > 100:
                            mime = dl_resp.headers.get("content-type", "image/png")
                            result.images.append(GeneratedImage(
                                data=dl_resp.content,
                                seed=seed_val,
                                mime_type=mime,
                                media_id=media_id,
                            ))
                            logger.info(f"Downloaded image from fifeUrl ({len(dl_resp.content)} bytes)")
                        else:
                            logger.warning(f"Failed to download fifeUrl: status={dl_resp.status_code}")
                    except Exception as e:
                        logger.error(f"Error downloading fifeUrl: {e}")

            # Format B: {generatedImages: [{image: {imageBytes, mimeType}, seed}]}
            if not result.images:
                generated_images = (
                    data.get("generatedImages")
                    or data.get("images")
                    or data.get("results")
                    or []
                )
                for img_data in generated_images:
                    if isinstance(img_data, dict):
                        image_obj = img_data.get("image", img_data)
                        image_bytes_b64 = (
                            image_obj.get("imageBytes")
                            or image_obj.get("encodedImage")
                            or image_obj.get("data")
                            or image_obj.get("base64")
                        )
                        if image_bytes_b64:
                            raw = base64.b64decode(image_bytes_b64)
                            seed = img_data.get("seed") or img_data.get("generationSeed")
                            mime = image_obj.get("mimeType", "image/png")
                            result.images.append(GeneratedImage(data=raw, seed=seed, mime_type=mime))

            # Check for filtered/safety response
            if not result.images:
                resp_str = json.dumps(data)
                if any(kw in resp_str.upper() for kw in ("FILTER", "SAFETY", "BLOCKED", "VIOLAT")):
                    result.filtered = True
                    result.error_code = "FILTERED"
                    result.error_message = "Content was filtered by safety systems"
                else:
                    logger.warning(f"No images parsed from response: {resp_str[:1000]}")

            if result.images:
                logger.info(f"Parsed {len(result.images)} images from response")

        except Exception as e:
            logger.error(f"Error parsing image response: {e}")
            result.error_code = "PARSE_ERROR"
            result.error_message = str(e)

        return result

    async def upsample_image(
        self,
        at: str,
        project_id: str,
        media_id: str,
        target_resolution: str = "UPSAMPLE_IMAGE_RESOLUTION_2K",
        user_paygate_tier: Optional[str] = None,
    ) -> bytes:
        """
        Upsample a generated image via /v1/flow/upsampleImage.

        Args:
            at: Access token
            project_id: Google project ID
            media_id: Media UUID from generation response (media[].name)
            target_resolution: UPSAMPLE_IMAGE_RESOLUTION_2K or UPSAMPLE_IMAGE_RESOLUTION_4K
            user_paygate_tier: Account tier

        Returns:
            Raw image bytes (decoded from base64 encodedImage)
        """
        session = await self._get_session()
        headers = self._get_headers(at)
        headers["content-type"] = "text/plain;charset=UTF-8"

        session_id = f";{int(time.time() * 1000)}"

        recaptcha_token = ""
        if self.capsolver:
            try:
                recaptcha_token = await self.capsolver.solve_recaptcha_v3(
                    website_url=f"{WEBSITE_URL}/fx/tools/flow/project/{project_id}",
                    website_key=RECAPTCHA_KEY,
                    page_action="IMAGE_UPSAMPLE",
                )
            except CapsolverError as e:
                logger.warning(f"reCAPTCHA failed for upsample: {e}")

        client_context: dict = {
            "projectId": project_id,
            "tool": "PINHOLE",
            "userPaygateTier": user_paygate_tier or "PAYGATE_TIER_ONE",
            "sessionId": session_id,
        }
        if recaptcha_token:
            client_context["recaptchaContext"] = {
                "token": recaptcha_token,
                "applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB",
            }

        payload = {
            "mediaId": media_id,
            "targetResolution": target_resolution,
            "clientContext": client_context,
        }

        url = f"{BASE_URL}/v1/flow/upsampleImage"
        payload_str = json.dumps(payload, separators=(",", ":"))

        logger.info(f"Upsampling image {media_id} → {target_resolution}")
        response = await self._request_with_retry("POST", url, headers=headers, data=payload_str)

        if response.status_code != 200:
            raise ImageClientError(
                f"Upsample failed: {response.status_code} {response.text[:300]}",
                error_code="UPSAMPLE_FAILED",
                retry=True,
            )

        data = response.json()
        encoded = data.get("encodedImage")
        if not encoded:
            raise ImageClientError(
                f"No encodedImage in upsample response: {json.dumps(data)[:300]}",
                error_code="UPSAMPLE_NO_DATA",
            )

        raw = base64.b64decode(encoded)
        logger.info(f"Upsample complete: {media_id} ({len(raw)} bytes)")
        return raw

    @staticmethod
    def _find_public_error(blob: Any) -> Optional[str]:
        """Recursively search for PUBLIC_ERROR_* string."""
        if isinstance(blob, str):
            if "PUBLIC_ERROR" in blob:
                m = re.search(r'(PUBLIC_ERROR\w+)', blob)
                return m.group(1) if m else None
            return None
        if isinstance(blob, dict):
            for v in blob.values():
                found = ImageClient._find_public_error(v)
                if found:
                    return found
        if isinstance(blob, list):
            for item in blob:
                found = ImageClient._find_public_error(item)
                if found:
                    return found
        return None
