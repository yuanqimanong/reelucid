"""Validate saved settings and capture the credentials a job may use."""

from reelucid.domain import JsonObject, ReelucidError
from reelucid.files import digest_json
from reelucid.settings import ProcessingProfile, SystemSettings


def settings_value(value: JsonObject) -> SystemSettings:
    return SystemSettings.model_validate(
        {key: item for key, item in value.items() if key != "secrets"}
    )


def snapshot(value: JsonObject) -> JsonObject:
    return {
        **settings_value(value).model_dump(),
        "secret_fingerprints": {
            key: digest_json(encrypted) for key, encrypted in value.get("secrets", {}).items()
        },
    }


def find_profile(settings: SystemSettings, identifier: str) -> ProcessingProfile:
    for profile in settings.processing_profiles:
        if profile.id == identifier:
            return profile
    raise ReelucidError("PROFILE_NOT_FOUND", "处理预设不存在。")
