"""Benchmark image version pinning.

The harness pins benchmark images to a data-version tag via the
EVOCLAW_IMAGE_TAG environment variable (default: v0.9).

Resolution rules (deliberately loud, never silent):
- If <image>:<pinned> exists locally, use it.
- If the pin came from the DEFAULT (env var not set) and <image>:latest exists,
  fall back to :latest with a prominent warning — convenience for hosts whose
  local images predate versioned tags. The content is NOT verified.
- If EVOCLAW_IMAGE_TAG is set explicitly, never fall back: reproducibility
  runs must fail fast rather than grade against the wrong data version.
"""

import os
import subprocess

DEFAULT_IMAGE_TAG = "v0.9"


def _image_exists(ref: str) -> bool:
    return (
        subprocess.run(
            ["docker", "image", "inspect", ref],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode
        == 0
    )


def resolve_image(image_base: str) -> str:
    """Return a fully-tagged image ref for an untagged image name.

    image_base must not contain a tag (e.g. "repo_full/milestone_001").
    """
    env_tag = os.environ.get("EVOCLAW_IMAGE_TAG")
    tag = env_tag or DEFAULT_IMAGE_TAG
    ref = f"{image_base}:{tag}"
    if _image_exists(ref):
        return ref
    if env_tag is None and _image_exists(f"{image_base}:latest"):
        print(
            f"⚠️  WARNING: {ref} not found locally; falling back to "
            f"{image_base}:latest (content unverified — run "
            f"scripts/pull_images.sh or tag your images, see EVOCLAW_IMAGE_TAG)"
        )
        return f"{image_base}:latest"
    # Let the caller fail with its normal image-not-found handling.
    return ref
