"""
Unit Tests for Workflow Engine
"""
import pytest
from unittest.mock import Mock, patch, AsyncMock
from datetime import datetime
from bson import ObjectId

from app.services.integrations.workflow_engine import WorkflowEngine
from app.models.workflow import (
    Workflow, WorkflowAction, WorkflowCondition,
    TriggerEvent, ActionType, ConditionOperator
)
from app.models.integration import Integration, IntegrationType


class TestWorkflowEngine:
    """Test suite for workflow engine"""

    # Pre-generated valid ObjectId strings for consistent test fixtures
    EMAIL_INTEGRATION_ID = "507f1f77bcf86cd799439011"
    JIRA_INTEGRATION_ID = "507f1f77bcf86cd799439012"
    HUBSPOT_INTEGRATION_ID = "507f1f77bcf86cd799439013"
    MISSING_INTEGRATION_ID = "507f1f77bcf86cd799439099"

    @pytest.fixture
    def mock_db(self):
        """Mock database"""
        db = Mock()
        db.workflows = Mock()
        db.workflow_executions = Mock()
        db.integrations = Mock()
        db.integration_logs = Mock()
        return db

    @pytest.fixture
    def workflow_engine(self, mock_db):
        """Workflow engine with mocked database"""
        with patch('app.services.integrations.workflow_engine.Database') as mock_database:
            mock_database.get_db.return_value = mock_db
            engine = WorkflowEngine()
            engine.db = mock_db
            return engine

    @pytest.fixture
    def sample_workflow(self):
        """Sample workflow for testing"""
        return Workflow(
            _id=str(ObjectId()),
            user_id="user123",
            name="Test Workflow",
            trigger_event=TriggerEvent.CALL_COMPLETED,
            conditions=[],
            actions=[
                WorkflowAction(
                    id="action1",
                    type=ActionType.SEND_EMAIL,
                    integration_id=self.EMAIL_INTEGRATION_ID,
                    config={
                        "to": "test@example.com",
                        "subject": "Test",
                        "body": "Test body"
                    }
                )
            ]
        )

    @pytest.fixture
    def sample_integration(self):
        """Sample integration for testing"""
        return Integration(
            _id=self.EMAIL_INTEGRATION_ID,
            user_id="user123",
            name="Test Email",
            type=IntegrationType.EMAIL,
            credentials={
                "smtp_host": "smtp.gmail.com",
                "smtp_port": 587,
                "smtp_username": "test@example.com",
                "smtp_password": "password",
                "from_email": "test@example.com",
                "use_tls": True
            },
            is_active=True
        )

    @pytest.mark.asyncio
    async def test_trigger_workflow_finds_matching_workflows(self, workflow_engine, mock_db, sample_workflow):
        """Test trigger finds matching workflows"""
        mock_db.workflows.find.return_value.sort.return_value = [sample_workflow.dict(by_alias=True)]
        mock_db.workflow_executions.insert_one = Mock()
        mock_db.workflows.update_one = Mock()
        mock_db.integrations.find_one.return_value = None

        trigger_data = {"call": {"duration": 120}}

        with patch.object(workflow_engine, 'execute_workflow', new_callable=AsyncMock) as mock_execute:
            mock_execute.return_value = {"success": True}
            results = await workflow_engine.trigger_workflow(
                TriggerEvent.CALL_COMPLETED,
                trigger_data,
                "user123"
            )

        assert len(results) == 1
        mock_execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_execute_workflow_with_no_conditions(self, workflow_engine, mock_db, sample_workflow, sample_integration):
        """Test workflow execution with no conditions"""
        mock_db.workflow_executions.insert_one = Mock()
        mock_db.workflows.update_one = Mock()
        mock_db.integrations.find_one.return_value = sample_integration.dict(by_alias=True)
        mock_db.integration_logs.insert_one = Mock()

        trigger_data = {"call": {"duration": 120}}

        with patch('app.services.integrations.workflow_engine.EmailService') as mock_email:
            mock_email_instance = Mock()
            mock_email_instance.send_email.return_value = {"success": True}
            mock_email.return_value = mock_email_instance

            result = await workflow_engine.execute_workflow(sample_workflow, trigger_data)

        assert result["success"] is True
        assert result["workflow_id"] == sample_workflow.id

    @pytest.mark.asyncio
    async def test_execute_workflow_conditions_met(self, workflow_engine, mock_db, sample_workflow, sample_integration):
        """Test workflow with conditions that are met"""
        sample_workflow.conditions = [
            WorkflowCondition(
                field="call.duration",
                operator=ConditionOperator.GREATER_THAN,
                value=60
            )
        ]

        mock_db.workflow_executions.insert_one = Mock()
        mock_db.workflows.update_one = Mock()
        mock_db.integrations.find_one.return_value = sample_integration.dict(by_alias=True)
        mock_db.integration_logs.insert_one = Mock()

        trigger_data = {"call": {"duration": 120}}

        with patch('app.services.integrations.workflow_engine.EmailService') as mock_email:
            mock_email_instance = Mock()
            mock_email_instance.send_email.return_value = {"success": True}
            mock_email.return_value = mock_email_instance

            result = await workflow_engine.execute_workflow(sample_workflow, trigger_data)

        assert result["success"] is True
        assert result["conditions_met"] is True

    @pytest.mark.asyncio
    async def test_execute_workflow_conditions_not_met(self, workflow_engine, mock_db, sample_workflow):
        """Test workflow with conditions that are not met"""
        sample_workflow.conditions = [
            WorkflowCondition(
                field="call.duration",
                operator=ConditionOperator.GREATER_THAN,
                value=200
            )
        ]

        mock_db.workflow_executions.insert_one = Mock()
        mock_db.workflows.update_one = Mock()

        trigger_data = {"call": {"duration": 120}}

        result = await workflow_engine.execute_workflow(sample_workflow, trigger_data)

        assert result["success"] is True
        assert result["conditions_met"] is False

    @pytest.mark.asyncio
    async def test_execute_action_jira(self, workflow_engine, mock_db):
        """Test executing Jira action"""
        action = WorkflowAction(
            id="jira-action",
            type=ActionType.CREATE_JIRA_TICKET,
            integration_id=self.JIRA_INTEGRATION_ID,
            config={
                "project": "TEST",
                "summary": "Test ticket",
                "description": "Test description"
            }
        )

        jira_integration = Integration(
            _id=self.JIRA_INTEGRATION_ID,
            user_id="user123",
            name="Test Jira",
            type=IntegrationType.JIRA,
            credentials={
                "base_url": "https://test.atlassian.net",
                "email": "test@example.com",
                "api_token": "token"
            },
            is_active=True
        )

        mock_db.integrations.find_one.return_value = jira_integration.dict(by_alias=True)
        mock_db.integration_logs.insert_one = Mock()

        context_data = {}

        with patch('app.services.integrations.workflow_engine.JiraService') as mock_jira:
            mock_jira_instance = Mock()
            mock_jira_instance.create_issue.return_value = {
                "success": True,
                "issue_key": "TEST-123"
            }
            mock_jira.return_value = mock_jira_instance

            result = await workflow_engine.execute_action(action, context_data, "user123")

        assert result["success"] is True
        assert result["action_type"] == ActionType.CREATE_JIRA_TICKET

    @pytest.mark.asyncio
    async def test_execute_action_hubspot(self, workflow_engine, mock_db):
        """Test executing HubSpot action"""
        action = WorkflowAction(
            id="hubspot-action",
            type=ActionType.CREATE_HUBSPOT_CONTACT,
            integration_id=self.HUBSPOT_INTEGRATION_ID,
            config={
                "email": "test@example.com",
                "firstname": "John"
            }
        )

        hubspot_integration = Integration(
            _id=self.HUBSPOT_INTEGRATION_ID,
            user_id="user123",
            name="Test HubSpot",
            type=IntegrationType.HUBSPOT,
            credentials={
                "access_token": "token",
                "portal_id": "12345"
            },
            is_active=True
        )

        mock_db.integrations.find_one.return_value = hubspot_integration.dict(by_alias=True)
        mock_db.integration_logs.insert_one = Mock()

        context_data = {}

        with patch('app.services.integrations.workflow_engine.HubSpotService') as mock_hubspot:
            mock_hubspot_instance = Mock()
            mock_hubspot_instance.create_contact.return_value = {
                "success": True,
                "contact_id": "456"
            }
            mock_hubspot.return_value = mock_hubspot_instance

            result = await workflow_engine.execute_action(action, context_data, "user123")

        assert result["success"] is True
        assert result["action_type"] == ActionType.CREATE_HUBSPOT_CONTACT

    @pytest.mark.asyncio
    async def test_execute_action_email(self, workflow_engine, mock_db):
        """Test executing Email action"""
        action = WorkflowAction(
            id="email-action",
            type=ActionType.SEND_EMAIL,
            integration_id=self.EMAIL_INTEGRATION_ID,
            config={
                "to": "recipient@example.com",
                "subject": "Test",
                "body": "Test body"
            }
        )

        email_integration = Integration(
            _id=self.EMAIL_INTEGRATION_ID,
            user_id="user123",
            name="Test Email",
            type=IntegrationType.EMAIL,
            credentials={
                "smtp_host": "smtp.gmail.com",
                "smtp_port": 587,
                "smtp_username": "test@example.com",
                "smtp_password": "password",
                "from_email": "test@example.com",
                "use_tls": True
            },
            is_active=True
        )

        mock_db.integrations.find_one.return_value = email_integration.dict(by_alias=True)
        mock_db.integration_logs.insert_one = Mock()

        context_data = {}

        with patch('app.services.integrations.workflow_engine.EmailService') as mock_email:
            mock_email_instance = Mock()
            mock_email_instance.send_email.return_value = {
                "success": True,
                "to": ["recipient@example.com"]
            }
            mock_email.return_value = mock_email_instance

            result = await workflow_engine.execute_action(action, context_data, "user123")

        assert result["success"] is True
        assert result["action_type"] == ActionType.SEND_EMAIL

    @pytest.mark.asyncio
    async def test_execute_action_integration_not_found(self, workflow_engine, mock_db):
        """Test action execution fails when integration not found"""
        action = WorkflowAction(
            id="test-action",
            type=ActionType.SEND_EMAIL,
            integration_id=self.MISSING_INTEGRATION_ID,
            config={"to": "test@example.com"}
        )

        mock_db.integrations.find_one.return_value = None

        context_data = {}

        result = await workflow_engine.execute_action(action, context_data, "user123")

        assert result["success"] is False
        assert "not found" in result["error_message"]

    @pytest.mark.asyncio
    async def test_execute_workflow_multiple_actions(self, workflow_engine, mock_db, sample_integration):
        """Test workflow with multiple actions"""
        workflow = Workflow(
            _id=str(ObjectId()),
            user_id="user123",
            name="Multi-Action Workflow",
            trigger_event=TriggerEvent.CALL_COMPLETED,
            conditions=[],
            actions=[
                WorkflowAction(
                    id="action1",
                    type=ActionType.SEND_EMAIL,
                    integration_id=self.EMAIL_INTEGRATION_ID,
                    config={"to": "test1@example.com", "subject": "Test1", "body": "Body1"}
                ),
                WorkflowAction(
                    id="action2",
                    type=ActionType.SEND_EMAIL,
                    integration_id=self.EMAIL_INTEGRATION_ID,
                    config={"to": "test2@example.com", "subject": "Test2", "body": "Body2"}
                )
            ]
        )

        mock_db.workflow_executions.insert_one = Mock()
        mock_db.workflows.update_one = Mock()
        mock_db.integrations.find_one.return_value = sample_integration.dict(by_alias=True)
        mock_db.integration_logs.insert_one = Mock()

        trigger_data = {}

        with patch('app.services.integrations.workflow_engine.EmailService') as mock_email:
            mock_email_instance = Mock()
            mock_email_instance.send_email.return_value = {"success": True}
            mock_email.return_value = mock_email_instance

            result = await workflow_engine.execute_workflow(workflow, trigger_data)

        assert result["success"] is True
        assert result["actions_executed"] == 2
        assert result["actions_succeeded"] == 2

    @pytest.mark.asyncio
    async def test_execute_workflow_action_failure_continue(self, workflow_engine, mock_db, sample_integration):
        """Test workflow continues on action failure when on_error=continue"""
        workflow = Workflow(
            _id=str(ObjectId()),
            user_id="user123",
            name="Error Handling Workflow",
            trigger_event=TriggerEvent.CALL_COMPLETED,
            conditions=[],
            actions=[
                WorkflowAction(
                    id="action1",
                    type=ActionType.SEND_EMAIL,
                    integration_id=self.EMAIL_INTEGRATION_ID,
                    config={"to": "test1@example.com"},
                    on_error="continue"
                ),
                WorkflowAction(
                    id="action2",
                    type=ActionType.SEND_EMAIL,
                    integration_id=self.EMAIL_INTEGRATION_ID,
                    config={"to": "test2@example.com"},
                    on_error="continue"
                )
            ]
        )

        mock_db.workflow_executions.insert_one = Mock()
        mock_db.workflows.update_one = Mock()
        mock_db.integrations.find_one.return_value = sample_integration.dict(by_alias=True)
        mock_db.integration_logs.insert_one = Mock()

        trigger_data = {}

        with patch('app.services.integrations.workflow_engine.EmailService') as mock_email:
            mock_email_instance = Mock()
            # First action fails, second succeeds
            mock_email_instance.send_email.side_effect = [
                {"success": False, "error": "Failed"},
                {"success": True}
            ]
            mock_email.return_value = mock_email_instance

            result = await workflow_engine.execute_workflow(workflow, trigger_data)

        # Workflow should complete with partial success
        assert result["actions_executed"] == 2
        assert result["actions_succeeded"] == 1

    @pytest.mark.asyncio
    async def test_workflow_execution_saves_logs(self, workflow_engine, mock_db, sample_workflow, sample_integration):
        """Test that workflow execution is properly logged"""
        mock_db.workflow_executions.insert_one = Mock()
        mock_db.workflows.update_one = Mock()
        mock_db.integrations.find_one.return_value = sample_integration.dict(by_alias=True)
        mock_db.integration_logs.insert_one = Mock()

        trigger_data = {"call": {"duration": 120}}

        with patch('app.services.integrations.workflow_engine.EmailService') as mock_email:
            mock_email_instance = Mock()
            mock_email_instance.send_email.return_value = {"success": True}
            mock_email.return_value = mock_email_instance

            await workflow_engine.execute_workflow(sample_workflow, trigger_data)

        # Verify execution was logged
        mock_db.workflow_executions.insert_one.assert_called_once()

        # Verify workflow statistics were updated
        mock_db.workflows.update_one.assert_called_once()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
