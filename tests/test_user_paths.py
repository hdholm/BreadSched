"""Platform-specific user directories without a third-party dependency."""

from breadsched.gen.utils.user_paths import (
    config_directory,
    documents_directory,
    sync_service_for_path,
)


def test_windows_config_uses_appdata(tmp_path):
    appdata = tmp_path / "Roaming"
    assert (
        config_directory(environ={"APPDATA": str(appdata)}, home=tmp_path, platform="win32")
        == appdata / "breadsched"
    )


def test_macos_config_uses_application_support(tmp_path):
    assert config_directory(environ={}, home=tmp_path, platform="darwin") == (
        tmp_path / "Library" / "Application Support" / "breadsched"
    )


def test_xdg_documents_file_is_honoured(tmp_path):
    config = tmp_path / "config"
    config.mkdir()
    documents = tmp_path / "Household Files"
    documents.mkdir()
    (config / "user-dirs.dirs").write_text(
        'XDG_DOCUMENTS_DIR="$HOME/Household Files"\n', encoding="utf-8"
    )
    assert (
        documents_directory(
            environ={"XDG_CONFIG_HOME": str(config)}, home=tmp_path, platform="linux"
        )
        == documents
    )


def test_windows_onedrive_documents_is_detected(tmp_path):
    onedrive = tmp_path / "OneDrive"
    documents = onedrive / "Documents"
    documents.mkdir(parents=True)
    env = {"OneDrive": str(onedrive)}
    assert documents_directory(environ=env, home=tmp_path, platform="win32") == documents
    assert sync_service_for_path(documents / "book.breadsched", environ=env, home=tmp_path) == (
        "OneDrive"
    )


def test_local_path_is_not_misidentified_as_synced(tmp_path):
    local = tmp_path / "Documents" / "book.breadsched"
    assert sync_service_for_path(local, environ={}, home=tmp_path) is None
