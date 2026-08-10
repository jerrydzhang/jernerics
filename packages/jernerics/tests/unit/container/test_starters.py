import pytest
from jernerics.container.templates import get_starter, list_starters


class TestStarters:
    def test_list_starters(self):
        starters = list_starters()
        assert "python" in starters

    def test_get_python_starter(self):
        starter = get_starter("python")
        assert "Bootstrap: docker" in starter
        assert "python:3.12" in starter

    def test_python_starter_reactivates_venv_for_login_shells(self):
        """The venv must survive login subshells (tmux panes re-run /etc/profile
        and would otherwise drop /opt/venv from PATH, breaking `import torch`).
        Both starters must bake activation into /etc/profile.d."""
        for ext in (".def", ".Dockerfile"):
            starter = get_starter("python", ext=ext)
            assert "/etc/profile.d/jernerics.sh" in starter, ext
            assert "/opt/venv/bin" in starter, ext
            assert "/etc/bash.bashrc" in starter, ext

    def test_python_starter_includes_readme(self):
        starter = get_starter("python")
        assert "README.md" in starter

    def test_get_invalid_starter(self):
        with pytest.raises(ValueError, match="not found"):
            get_starter("nonexistent")

    def test_rejects_path_traversal(self):
        with pytest.raises(ValueError, match="Invalid starter name"):
            get_starter("../../../etc/passwd")

    def test_rejects_path_separator(self):
        with pytest.raises(ValueError, match="Invalid starter name"):
            get_starter("foo/bar")

    def test_rejects_dot_dot(self):
        with pytest.raises(ValueError, match="Invalid starter name"):
            get_starter("foo..bar")

    def test_accepts_valid_names(self):
        for name in ["python", "my-template", "my_template", "starter123"]:
            try:
                get_starter(name)
            except ValueError as e:
                if "Invalid starter name" in str(e):
                    raise AssertionError(f"Valid name '{name}' was rejected") from e
