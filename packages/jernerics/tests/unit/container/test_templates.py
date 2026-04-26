import pytest
from jernerics.container.templates import get_template, list_templates


class TestTemplates:
    def test_list_templates(self):
        templates = list_templates()
        assert "python" in templates

    def test_get_python_template(self):
        template = get_template("python")
        assert "Bootstrap: docker" in template
        assert "python:3.12" in template

    def test_python_template_includes_readme(self):
        template = get_template("python")
        assert "README.md" in template

    def test_get_invalid_template(self):
        with pytest.raises(ValueError, match="not found"):
            get_template("nonexistent")

    def test_rejects_path_traversal(self):
        with pytest.raises(ValueError, match="Invalid template name"):
            get_template("../../../etc/passwd")

    def test_rejects_path_separator(self):
        with pytest.raises(ValueError, match="Invalid template name"):
            get_template("foo/bar")

    def test_rejects_dot_dot(self):
        with pytest.raises(ValueError, match="Invalid template name"):
            get_template("foo..bar")

    def test_accepts_valid_names(self):
        for name in ["python", "my-template", "my_template", "template123"]:
            try:
                get_template(name)
            except ValueError as e:
                if "Invalid template name" in str(e):
                    raise AssertionError(f"Valid name '{name}' was rejected") from e
