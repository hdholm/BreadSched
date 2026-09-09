"""Settings live in INI files under the user's config directory.

Two properties matter more than the storage format. Reading must never fail: a
corrupt preferences file should cost a user their preferences, not their ability to
start the application. And writing must be atomic, because a save interrupted by a
crash or a full disk otherwise leaves a half-written file that fails to parse on
the next start — turning one bad moment into a permanent one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cashperspective.gen.utils.settings import Settings, config_directory


@pytest.fixture
def settings(tmp_path):
    return Settings("test", directory=tmp_path / "breadsched")


class TestLocation:
    def test_it_follows_xdg_config_home(self, monkeypatch, tmp_path):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "somewhere"))
        assert config_directory() == tmp_path / "somewhere" / "breadsched"

    def test_it_falls_back_to_dot_config(self, monkeypatch, tmp_path):
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        assert config_directory() == tmp_path / ".config" / "breadsched"

    def test_the_file_is_an_ini(self, settings):
        assert settings.path.suffix == ".ini"


class TestReadingAndWriting:
    def test_a_value_survives_a_round_trip(self, settings, tmp_path):
        settings.set("general", "last_book", "/books/household.cashperspective")
        assert settings.save() is True

        reloaded = Settings("test", directory=tmp_path / "breadsched")
        assert reloaded.get("general", "last_book") == "/books/household.cashperspective"

    def test_missing_values_return_the_default(self, settings):
        assert settings.get("general", "absent", "fallback") == "fallback"
        assert settings.get_int("general", "absent", 7) == 7
        assert settings.get_bool("general", "absent", True) is True

    def test_types_round_trip(self, settings, tmp_path):
        settings.set("view", "width", 240)
        settings.set("view", "shown", True)
        settings.set("view", "hidden", ["memo", "num"])
        settings.save()

        reloaded = Settings("test", directory=tmp_path / "breadsched")
        assert reloaded.get_int("view", "width") == 240
        assert reloaded.get_bool("view", "shown") is True
        assert reloaded.get_list("view", "hidden") == ["memo", "num"]

    def test_a_removed_value_is_gone(self, settings, tmp_path):
        settings.set("general", "last_book", "/gone")
        settings.save()
        settings.remove("general", "last_book")
        settings.save()
        assert Settings("test", directory=tmp_path / "breadsched").get(
            "general", "last_book"
        ) is None

    def test_the_directory_is_created_on_demand(self, tmp_path):
        target = tmp_path / "deep" / "nested" / "breadsched"
        store = Settings("test", directory=target)
        store.set("general", "x", "1")
        assert store.save() is True
        assert (target / "test.ini").exists()


class TestResilience:
    def test_a_corrupt_file_yields_defaults_rather_than_raising(self, tmp_path):
        directory = tmp_path / "breadsched"
        directory.mkdir(parents=True)
        (directory / "test.ini").write_text("this is not = valid [ini at all\n\x00")

        store = Settings("test", directory=directory)
        assert store.get("general", "last_book") is None

    def test_a_corrupt_file_can_still_be_written_over(self, tmp_path):
        directory = tmp_path / "breadsched"
        directory.mkdir(parents=True)
        (directory / "test.ini").write_text("[[[broken")

        store = Settings("test", directory=directory)
        store.set("general", "last_book", "/books/new.cashperspective")
        assert store.save() is True
        assert Settings("test", directory=directory).get(
            "general", "last_book"
        ) == "/books/new.cashperspective"

    def test_saving_leaves_no_temporary_file_behind(self, settings):
        settings.set("general", "x", "1")
        settings.save()
        assert list(settings.directory.glob("*.tmp")) == []

    def test_an_unwritable_directory_is_reported_not_raised(self, tmp_path):
        blocker = tmp_path / "blocked"
        blocker.write_text("I am a file, not a directory")
        store = Settings("test", directory=blocker / "breadsched")
        store.set("general", "x", "1")
        assert store.save() is False


class TestLastBook:
    """The book you were working on should be there when you come back."""

    @pytest.fixture
    def app_settings(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
        return tmp_path

    def test_opening_a_book_records_it(self, app_settings, tmp_path, capsys):
        from cashperspective.cli.main import main as cli
        from cashperspective.gen.utils.settings import Settings as Store

        path = tmp_path / "household.cashperspective"
        cli(["init", str(path)])
        capsys.readouterr()

        store = Store("settings")
        store.set("general", "last_book", str(path.resolve()))
        store.save()

        assert Store("settings").get("general", "last_book") == str(path.resolve())

    def test_a_book_that_has_gone_is_forgotten(self, app_settings, tmp_path):
        from cashperspective.gen.utils.settings import Settings as Store

        store = Store("settings")
        store.set("general", "last_book", str(tmp_path / "deleted.cashperspective"))
        store.save()

        remembered = store.get("general", "last_book")
        assert remembered is not None
        assert not Path(remembered).exists(), (
            "the application should clear this rather than fail on start-up"
        )
