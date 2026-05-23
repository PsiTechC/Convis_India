"""
Unit Tests for Condition Evaluator
"""
import pytest
from app.services.integrations.condition_evaluator import ConditionEvaluator
from app.models.workflow import WorkflowCondition, ConditionOperator


class TestConditionEvaluator:
    """Test suite for condition evaluation functionality"""

    # Test Basic Operators

    def test_equals_operator_true(self):
        """Test equals operator returns true"""
        condition = WorkflowCondition(
            field="status",
            operator=ConditionOperator.EQUALS,
            value="completed"
        )
        data = {"status": "completed"}

        result = ConditionEvaluator.evaluate_condition(condition, data)

        assert result is True

    def test_equals_operator_false(self):
        """Test equals operator returns false"""
        condition = WorkflowCondition(
            field="status",
            operator=ConditionOperator.EQUALS,
            value="completed"
        )
        data = {"status": "failed"}

        result = ConditionEvaluator.evaluate_condition(condition, data)

        assert result is False

    def test_not_equals_operator(self):
        """Test not equals operator"""
        condition = WorkflowCondition(
            field="status",
            operator=ConditionOperator.NOT_EQUALS,
            value="failed"
        )
        data = {"status": "completed"}

        result = ConditionEvaluator.evaluate_condition(condition, data)

        assert result is True

    def test_greater_than_operator(self):
        """Test greater than operator"""
        condition = WorkflowCondition(
            field="duration",
            operator=ConditionOperator.GREATER_THAN,
            value=60
        )
        data = {"duration": 120}

        result = ConditionEvaluator.evaluate_condition(condition, data)

        assert result is True

    def test_less_than_operator(self):
        """Test less than operator"""
        condition = WorkflowCondition(
            field="duration",
            operator=ConditionOperator.LESS_THAN,
            value=100
        )
        data = {"duration": 50}

        result = ConditionEvaluator.evaluate_condition(condition, data)

        assert result is True

    def test_greater_than_or_equal_operator(self):
        """Test greater than or equal operator"""
        condition = WorkflowCondition(
            field="score",
            operator=ConditionOperator.GREATER_THAN_OR_EQUAL,
            value=90
        )
        data = {"score": 90}

        result = ConditionEvaluator.evaluate_condition(condition, data)

        assert result is True

    def test_less_than_or_equal_operator(self):
        """Test less than or equal operator"""
        condition = WorkflowCondition(
            field="age",
            operator=ConditionOperator.LESS_THAN_OR_EQUAL,
            value=30
        )
        data = {"age": 25}

        result = ConditionEvaluator.evaluate_condition(condition, data)

        assert result is True

    # Test String Operators

    def test_contains_operator_string(self):
        """Test contains operator with string"""
        condition = WorkflowCondition(
            field="message",
            operator=ConditionOperator.CONTAINS,
            value="error"
        )
        data = {"message": "An error occurred"}

        result = ConditionEvaluator.evaluate_condition(condition, data)

        assert result is True

    def test_not_contains_operator(self):
        """Test not contains operator"""
        condition = WorkflowCondition(
            field="message",
            operator=ConditionOperator.NOT_CONTAINS,
            value="success"
        )
        data = {"message": "An error occurred"}

        result = ConditionEvaluator.evaluate_condition(condition, data)

        assert result is True

    def test_starts_with_operator(self):
        """Test starts with operator"""
        condition = WorkflowCondition(
            field="phone",
            operator=ConditionOperator.STARTS_WITH,
            value="+1"
        )
        data = {"phone": "+1234567890"}

        result = ConditionEvaluator.evaluate_condition(condition, data)

        assert result is True

    def test_ends_with_operator(self):
        """Test ends with operator"""
        condition = WorkflowCondition(
            field="email",
            operator=ConditionOperator.ENDS_WITH,
            value="@gmail.com"
        )
        data = {"email": "user@gmail.com"}

        result = ConditionEvaluator.evaluate_condition(condition, data)

        assert result is True

    # Test List Operators

    def test_in_operator(self):
        """Test in operator"""
        condition = WorkflowCondition(
            field="status",
            operator=ConditionOperator.IN,
            value=["completed", "success", "done"]
        )
        data = {"status": "completed"}

        result = ConditionEvaluator.evaluate_condition(condition, data)

        assert result is True

    def test_not_in_operator(self):
        """Test not in operator"""
        condition = WorkflowCondition(
            field="status",
            operator=ConditionOperator.NOT_IN,
            value=["failed", "error"]
        )
        data = {"status": "completed"}

        result = ConditionEvaluator.evaluate_condition(condition, data)

        assert result is True

    def test_contains_operator_list(self):
        """Test contains operator with list"""
        condition = WorkflowCondition(
            field="tags",
            operator=ConditionOperator.CONTAINS,
            value="urgent"
        )
        data = {"tags": ["important", "urgent", "customer"]}

        result = ConditionEvaluator.evaluate_condition(condition, data)

        assert result is True

    # Test Existence Operators

    def test_exists_operator_true(self):
        """Test exists operator returns true"""
        condition = WorkflowCondition(
            field="customer.email",
            operator=ConditionOperator.EXISTS,
            value=None
        )
        data = {"customer": {"email": "test@example.com"}}

        result = ConditionEvaluator.evaluate_condition(condition, data)

        assert result is True

    def test_exists_operator_false(self):
        """Test exists operator returns false"""
        condition = WorkflowCondition(
            field="customer.email",
            operator=ConditionOperator.EXISTS,
            value=None
        )
        data = {"customer": {}}

        result = ConditionEvaluator.evaluate_condition(condition, data)

        assert result is False

    def test_not_exists_operator(self):
        """Test not exists operator"""
        condition = WorkflowCondition(
            field="optional_field",
            operator=ConditionOperator.NOT_EXISTS,
            value=None
        )
        data = {"required_field": "value"}

        result = ConditionEvaluator.evaluate_condition(condition, data)

        assert result is True

    # Test Regex Operator

    def test_matches_regex_operator(self):
        """Test regex matching operator"""
        condition = WorkflowCondition(
            field="phone",
            operator=ConditionOperator.MATCHES_REGEX,
            value=r"^\+1\d{10}$"
        )
        data = {"phone": "+12345678901"}

        result = ConditionEvaluator.evaluate_condition(condition, data)

        assert result is True

    # Test Nested Value Extraction

    def test_get_nested_value_simple(self):
        """Test getting simple nested value"""
        data = {"user": {"name": "John"}}

        result = ConditionEvaluator.get_nested_value(data, "user.name")

        assert result == "John"

    def test_get_nested_value_deep(self):
        """Test getting deeply nested value"""
        data = {
            "user": {
                "profile": {
                    "contact": {
                        "email": "test@example.com"
                    }
                }
            }
        }

        result = ConditionEvaluator.get_nested_value(data, "user.profile.contact.email")

        assert result == "test@example.com"

    def test_get_nested_value_array_index(self):
        """Test getting value from array by index"""
        data = {"items": ["first", "second", "third"]}

        result = ConditionEvaluator.get_nested_value(data, "items.0")

        assert result == "first"

    def test_get_nested_value_missing_returns_none(self):
        """Test missing nested value returns None"""
        data = {"user": {"name": "John"}}

        result = ConditionEvaluator.get_nested_value(data, "user.email")

        assert result is None

    # Test Multiple Conditions

    def test_evaluate_conditions_all_pass_with_and(self):
        """Test multiple conditions with AND logic all pass"""
        conditions = [
            WorkflowCondition(
                field="duration",
                operator=ConditionOperator.GREATER_THAN,
                value=60,
                logic="AND"
            ),
            WorkflowCondition(
                field="status",
                operator=ConditionOperator.EQUALS,
                value="completed"
            )
        ]
        data = {"duration": 120, "status": "completed"}

        result = ConditionEvaluator.evaluate_conditions(conditions, data)

        assert result is True

    def test_evaluate_conditions_one_fails_with_and(self):
        """Test multiple conditions with AND logic one fails"""
        conditions = [
            WorkflowCondition(
                field="duration",
                operator=ConditionOperator.GREATER_THAN,
                value=60,
                logic="AND"
            ),
            WorkflowCondition(
                field="status",
                operator=ConditionOperator.EQUALS,
                value="completed"
            )
        ]
        data = {"duration": 30, "status": "completed"}

        result = ConditionEvaluator.evaluate_conditions(conditions, data)

        assert result is False

    def test_evaluate_conditions_with_or_logic(self):
        """Test multiple conditions with OR logic"""
        conditions = [
            WorkflowCondition(
                field="status",
                operator=ConditionOperator.EQUALS,
                value="completed",
                logic="OR"
            ),
            WorkflowCondition(
                field="status",
                operator=ConditionOperator.EQUALS,
                value="success"
            )
        ]
        data = {"status": "success"}

        result = ConditionEvaluator.evaluate_conditions(conditions, data)

        assert result is True

    def test_evaluate_conditions_mixed_and_or(self):
        """Test mixed AND/OR conditions"""
        conditions = [
            WorkflowCondition(
                field="duration",
                operator=ConditionOperator.GREATER_THAN,
                value=60,
                logic="AND"
            ),
            WorkflowCondition(
                field="status",
                operator=ConditionOperator.EQUALS,
                value="completed",
                logic="OR"
            ),
            WorkflowCondition(
                field="priority",
                operator=ConditionOperator.EQUALS,
                value="high"
            )
        ]
        # Test case: (duration > 60 AND status == completed) OR priority == high
        # With left-to-right evaluation: ((FALSE AND TRUE) OR TRUE) = (FALSE OR TRUE) = TRUE
        data = {"duration": 30, "status": "completed", "priority": "high"}

        result = ConditionEvaluator.evaluate_conditions(conditions, data)

        assert result is True  # (duration > 60 AND status == completed) OR (priority == high)

    def test_evaluate_empty_conditions_returns_true(self):
        """Test empty conditions list returns true"""
        conditions = []
        data = {"anything": "value"}

        result = ConditionEvaluator.evaluate_conditions(conditions, data)

        assert result is True

    # Test Real-World Scenarios

    def test_call_duration_filter(self):
        """Test realistic call duration filter"""
        condition = WorkflowCondition(
            field="call.duration",
            operator=ConditionOperator.GREATER_THAN,
            value=60
        )
        data = {"call": {"duration": 125}}

        result = ConditionEvaluator.evaluate_condition(condition, data)

        assert result is True

    def test_customer_email_exists(self):
        """Test customer email exists check"""
        condition = WorkflowCondition(
            field="customer.email",
            operator=ConditionOperator.EXISTS,
            value=None
        )
        data = {"customer": {"name": "John", "email": "john@example.com"}}

        result = ConditionEvaluator.evaluate_condition(condition, data)

        assert result is True

    def test_call_status_completed(self):
        """Test call status is completed"""
        conditions = [
            WorkflowCondition(
                field="call.status",
                operator=ConditionOperator.EQUALS,
                value="completed",
                logic="AND"
            ),
            WorkflowCondition(
                field="call.duration",
                operator=ConditionOperator.GREATER_THAN,
                value=30
            )
        ]
        data = {
            "call": {
                "status": "completed",
                "duration": 120
            }
        }

        result = ConditionEvaluator.evaluate_conditions(conditions, data)

        assert result is True

    def test_complex_workflow_filter(self):
        """Test complex real-world workflow filter"""
        conditions = [
            WorkflowCondition(
                field="call.duration",
                operator=ConditionOperator.GREATER_THAN,
                value=60,
                logic="AND"
            ),
            WorkflowCondition(
                field="call.status",
                operator=ConditionOperator.IN,
                value=["completed", "success"],
                logic="AND"
            ),
            WorkflowCondition(
                field="customer.email",
                operator=ConditionOperator.EXISTS,
                value=None
            )
        ]
        data = {
            "call": {
                "duration": 120,
                "status": "completed"
            },
            "customer": {
                "email": "customer@example.com"
            }
        }

        result = ConditionEvaluator.evaluate_conditions(conditions, data)

        assert result is True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
