"""Google Flow platform-specific enums and configuration mappings."""

from enum import Enum


class FlowModel(str, Enum):
    """Available Google Flow image generation models.

    Values are identifiers used internally; UI matching uses MODEL_UI_LABELS.
    """
    NANO_BANANA_PRO = "nano_banana_pro"
    NANO_BANANA_2 = "nano_banana_2"
    IMAGEN_4 = "imagen_4"


class FlowOrientation(str, Enum):
    """Image orientation / aspect ratio."""
    LANDSCAPE = "landscape"   # 16:9
    PORTRAIT = "portrait"     # 9:16


class FlowCount(int, Enum):
    """Number of images to generate per request."""
    X1 = 1
    X2 = 2
    X3 = 3
    X4 = 4


class FlowVideoModel(str, Enum):
    """Available Google Flow video generation models."""
    VEO_3_1_FAST = "veo_3.1_fast"
    VEO_3_1_LITE = "veo_3.1_lite"
    VEO_3_1_LITE_LOWER = "veo_3.1_lite_lower"
    VEO_3_1_QUALITY = "veo_3.1_quality"
    VEO_2_FAST = "veo_2_fast"
    VEO_2_QUALITY = "veo_2_quality"
    OMNI_FLASH = "omni_flash"


class FlowVideoMode(str, Enum):
    """Video creation mode."""
    FRAMES = "frames"
    INGREDIENTS = "ingredients"


class FlowVideoGenerationMode(str, Enum):
    """Generation mode: what inputs drive the video."""
    T2V = "t2v"            # text only
    I2V = "i2v"            # start image + text
    I2V_DUAL = "i2v_dual"  # start image + end image + text
    R2V = "r2v"            # reference images + text (Ingredients tab)
    EXTEND = "extend"      # extend an existing video


# ── Image model mappings ──

# Model enum -> UI dropdown label text for matching.
MODEL_UI_LABELS: dict[FlowModel, str] = {
    FlowModel.NANO_BANANA_PRO: "Nano Banana Pro",
    FlowModel.NANO_BANANA_2: "Nano Banana 2",
    FlowModel.IMAGEN_4: "Imagen 4",
}

# User-friendly name -> enum member mapping for CLI usage.
MODEL_NAMES: dict[str, FlowModel] = {
    "nano-banana-pro": FlowModel.NANO_BANANA_PRO,
    "nano-banana-2": FlowModel.NANO_BANANA_2,
    "imagen-4": FlowModel.IMAGEN_4,
}

# ── Orientation mappings ──

ORIENTATION_UI_LABELS: dict[FlowOrientation, str] = {
    FlowOrientation.LANDSCAPE: "Landscape",
    FlowOrientation.PORTRAIT: "Portrait",
}

# ── Count mappings ──

COUNT_UI_LABELS: dict[FlowCount, str] = {
    # Flow's count tabs use the "Nx" format ("1x", "2x" …) for both the
    # tab accessible name and the config button suffix. The previous
    # "xN" form was wrong and silently left the default x2 selected.
    FlowCount.X1: "1x",
    FlowCount.X2: "2x",
    FlowCount.X3: "3x",
    FlowCount.X4: "4x",
}

# ── Video model mappings ──

VIDEO_MODEL_UI_LABELS: dict[FlowVideoModel, str] = {
    FlowVideoModel.VEO_3_1_FAST: "Veo 3.1 - Fast",
    FlowVideoModel.VEO_3_1_LITE: "Veo 3.1 - Lite",
    FlowVideoModel.VEO_3_1_LITE_LOWER: "Veo 3.1 - Lite [Lower",
    FlowVideoModel.VEO_3_1_QUALITY: "Veo 3.1 - Quality",
    FlowVideoModel.VEO_2_FAST: "Veo 2 - Fast",
    FlowVideoModel.VEO_2_QUALITY: "Veo 2 - Quality",
    FlowVideoModel.OMNI_FLASH: "Omni Flash",
}

VIDEO_MODEL_NAMES: dict[str, FlowVideoModel] = {
    "veo-3.1-fast": FlowVideoModel.VEO_3_1_FAST,
    "veo-3.1-lite": FlowVideoModel.VEO_3_1_LITE,
    "veo-3.1-lite-lower": FlowVideoModel.VEO_3_1_LITE_LOWER,
    "veo-3.1-quality": FlowVideoModel.VEO_3_1_QUALITY,
    "veo-2-fast": FlowVideoModel.VEO_2_FAST,
    "veo-2-quality": FlowVideoModel.VEO_2_QUALITY,
    "omni-flash": FlowVideoModel.OMNI_FLASH,
    "omni_flash": FlowVideoModel.OMNI_FLASH,
}
