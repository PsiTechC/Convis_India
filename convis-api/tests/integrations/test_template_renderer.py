"""
Unit Tests for Template Renderer
"""
import pytest
from datetime import datetime
from app.services.integrations.template_renderer import TemplateRenderer


class TestTemplateRenderer:
    """Test suite for template rendering functionality"""

    def test_simple_variable_rendering(self):
        """Test basic variable substitution"""
        template = "Hello {{name}}"
        data = {"name": "John"}

        result = TemplateRenderer.render(template, data)

        assert result == "Hello John"

    def test_nested_variable_rendering(self):
        """Test nested object variable substitution"""
        template = "Hello {{customer.name}}"
        data = {"customer": {"name": "John Doe"}}

        result = TemplateRenderer.render(template, data)

        assert result == "Hello John Doe"

    def test_deep_nested_variable(self):
        """Test deeply nested variables"""
        template = "{{user.profile.contact.email}}"
        data = {
            "user": {
                "profile": {
                    "contact": {
                        "email": "test@example.com"
                    }
                }
            }
        }

        result = TemplateRenderer.render(template, data)

        assert result == "test@example.com"

    def test_missing_variable_returns_empty(self):
        """Test missing variables return empty string"""
        template = "Hello {{missing}}"
        data = {"name": "John"}

        result = TemplateRenderer.render(template, data)

        assert result == "Hello "

    def test_multiple_variables(self):
        """Test multiple variables in one template"""
        template = "{{first}} {{last}} - {{email}}"
        data = {"first": "John", "last": "Doe", "email": "john@example.com"}

        result = TemplateRenderer.render(template, data)

        assert result == "John Doe - john@example.com"

    # Filter Tests

    def test_upper_filter(self):
        """Test uppercase filter"""
        template = "{{name|upper}}"
        data = {"name": "john"}

        result = TemplateRenderer.render(template, data)

        assert result == "JOHN"

    def test_lower_filter(self):
        """Test lowercase filter"""
        template = "{{name|lower}}"
        data = {"name": "JOHN"}

        result = TemplateRenderer.render(template, data)

        assert result == "john"

    def test_title_filter(self):
        """Test title case filter"""
        template = "{{name|title}}"
        data = {"name": "john doe"}

        result = TemplateRenderer.render(template, data)

        assert result == "John Doe"

    def test_truncate_filter(self):
        """Test truncate filter"""
        template = "{{text|truncate:10}}"
        data = {"text": "This is a very long text"}

        result = TemplateRenderer.render(template, data)

        assert result == "This is a ..."

    def test_round_filter(self):
        """Test round filter"""
        template = "{{value|round:2}}"
        data = {"value": 3.14159}

        result = TemplateRenderer.render(template, data)

        assert result == "3.14"

    def test_default_filter(self):
        """Test default value filter"""
        template = "{{missing|default:N/A}}"
        data = {}

        result = TemplateRenderer.render(template, data)

        assert result == "N/A"

    def test_date_filter(self):
        """Test date formatting filter"""
        template = "{{timestamp|date}}"
        data = {"timestamp": datetime(2024, 1, 15, 10, 30, 0)}

        result = TemplateRenderer.render(template, data)

        assert result == "2024-01-15"

    def test_datetime_filter(self):
        """Test datetime formatting filter"""
        template = "{{timestamp|datetime}}"
        data = {"timestamp": datetime(2024, 1, 15, 10, 30, 45)}

        result = TemplateRenderer.render(template, data)

        assert result == "2024-01-15 10:30:45"

    def test_custom_date_format_filter(self):
        """Test custom date format filter"""
        template = "{{timestamp|date:%Y/%m/%d}}"
        data = {"timestamp": datetime(2024, 1, 15)}

        result = TemplateRenderer.render(template, data)

        assert result == "2024/01/15"

    def test_chained_filters(self):
        """Test multiple filters chained together"""
        template = "{{text|lower|truncate:5}}"
        data = {"text": "HELLO WORLD"}

        result = TemplateRenderer.render(template, data)

        assert result == "hello..."

    def test_join_filter_with_list(self):
        """Test join filter on lists"""
        template = "{{tags|join}}"
        data = {"tags": ["python", "testing", "automation"]}

        result = TemplateRenderer.render(template, data)

        assert result == "python, testing, automation"

    def test_join_filter_with_custom_separator(self):
        """Test join filter with custom separator"""
        template = "{{tags|join: - }}"
        data = {"tags": ["python", "testing", "automation"]}

        result = TemplateRenderer.render(template, data)

        assert result == "python - testing - automation"

    def test_first_filter(self):
        """Test first element filter"""
        template = "{{items|first}}"
        data = {"items": ["apple", "banana", "cherry"]}

        result = TemplateRenderer.render(template, data)

        assert result == "apple"

    def test_last_filter(self):
        """Test last element filter"""
        template = "{{items|last}}"
        data = {"items": ["apple", "banana", "cherry"]}

        result = TemplateRenderer.render(template, data)

        assert result == "cherry"

    def test_length_filter(self):
        """Test length filter"""
        template = "{{text|length}}"
        data = {"text": "hello"}

        result = TemplateRenderer.render(template, data)

        assert result == "5"

    def test_int_filter(self):
        """Test integer conversion filter"""
        template = "{{value|int}}"
        data = {"value": "42"}

        result = TemplateRenderer.render(template, data)

        assert result == "42"

    def test_float_filter(self):
        """Test float conversion filter"""
        template = "{{value|float}}"
        data = {"value": "3.14"}

        result = TemplateRenderer.render(template, data)

        assert result == "3.14"

    def test_render_dict(self):
        """Test rendering entire dictionaries"""
        template_dict = {
            "title": "Hello {{name}}",
            "message": "Your score is {{score|round:0}}"
        }
        data = {"name": "John", "score": 95.7}

        result = TemplateRenderer.render_dict(template_dict, data)

        assert result["title"] == "Hello John"
        assert result["message"] == "Your score is 96"

    def test_render_dict_nested(self):
        """Test rendering nested dictionaries"""
        template_dict = {
            "user": {
                "name": "{{customer.name}}",
                "email": "{{customer.email}}"
            }
        }
        data = {"customer": {"name": "John", "email": "john@example.com"}}

        result = TemplateRenderer.render_dict(template_dict, data)

        assert result["user"]["name"] == "John"
        assert result["user"]["email"] == "john@example.com"

    def test_empty_template(self):
        """Test empty template returns empty string"""
        template = ""
        data = {"name": "John"}

        result = TemplateRenderer.render(template, data)

        assert result == ""

    def test_template_without_variables(self):
        """Test template with no variables"""
        template = "Hello World"
        data = {"name": "John"}

        result = TemplateRenderer.render(template, data)

        assert result == "Hello World"

    def test_complex_real_world_template(self):
        """Test complex real-world use case"""
        template = """
Call Summary for {{customer.name|upper}}

Duration: {{call.duration|round:0}} seconds
Status: {{call.status|title}}
Transcript: {{call.transcription|truncate:100}}
Date: {{call.created_at|datetime}}

Customer: {{customer.name}}
Email: {{customer.email|default:Not provided}}
        """.strip()

        data = {
            "customer": {
                "name": "john doe",
                "email": ""
            },
            "call": {
                "duration": 125.7,
                "status": "completed",
                "transcription": "This is a long transcription that needs to be truncated because it's too long for the summary display",
                "created_at": datetime(2024, 1, 15, 14, 30, 0)
            }
        }

        result = TemplateRenderer.render(template, data)

        assert "JOHN DOE" in result
        assert "126 seconds" in result
        assert "Completed" in result
        assert "..." in result  # Truncation
        assert "2024-01-15 14:30:00" in result
        assert "Not provided" in result


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
