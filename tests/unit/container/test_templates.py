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

    def test_get_invalid_template(self):
        with pytest.raises(ValueError, match="not found"):
            get_template("nonexistent")
