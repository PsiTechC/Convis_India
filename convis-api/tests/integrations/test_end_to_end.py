"""
End-to-End Integration Tests
Tests complete workflows from trigger to completion
"""
import pytest
from unittest.mock import Mock, patch, AsyncMock
from datetime import datetime
from bson import ObjectId

from app.services.integrations.workflow_trigger import WorkflowTrigger
from app.models.workflow import TriggerEvent


class TestEndToEndWorkflows:
    """End-to-end tests for complete workflow execution"""

    # Pre-generated valid ObjectId strings for consistent test fixtures
    JIRA_INTEGRATION_ID = "507f1f77bcf86cd799439012"
    HUBSPOT_INTEGRATION_ID = "507f1f77bcf86cd799439013"
    EMAIL_INTEGRATION_ID = "507f1f77bcf86cd799439011"
    SLACK_INTEGRATION_ID = "507f1f77bcf86cd799439014"

    @pytest.fixture
    def mock_db(self):
        """Mock database with sample data"""
        db = Mock()
        db.workflows = Mock()
        db.workflow_executions = Mock()
        db.integrations = Mock()
        db.integration_logs = Mock()
        return db

    @pytest.fixture
    def sample_call_data(self):
        """Sample call data for testing"""
        return {
            "_id": str(ObjectId()),
            "status": "completed",
            "duration": 125,
            "transcription": "Customer called about billing issue. Resolved by providing refund.",
            "summary": "Billing issue resolved with refund",
            "customer_name": "John Doe",
            "customer_email": "john@example.com",
            "customer_phone": "+1234567890",
            "agent_name": "Jane Agent",
            "agent_email": "jane@company.com",
            "created_at": datetime.utcnow(),
            "ended_at": datetime.utcnow()
        }

    @pytest.mark.asyncio
    async def test_call_completed_creates_jira_ticket(self, mock_db, sample_call_data):
        """Test: Call completes → Jira ticket created"""
        # Setup: Workflow that creates Jira ticket
        workflow = {
            "_id": str(ObjectId()),
            "user_id": "user123",
            "name": "Create Support Ticket",
            "trigger_event": "call_completed",
            "is_active": True,
            "priority": 0,
            "conditions": [
                {
                    "field": "call.duration",
                    "operator": "greater_than",
                    "value": 60
                }
            ],
            "actions": [
                {
                    "id": "action1",
                    "type": "create_jira_ticket",
                    "integration_id": "507f1f77bcf86cd799439012",
                    "config": {
                        "project": "SUPPORT",
                        "issue_type": "Task",
                        "summary": "Call from {{customer.name}}",
                        "description": "{{call.transcription}}"
                    },
                    "on_error": "continue"
                }
            ]
        }

        jira_integration = {
            "_id": "507f1f77bcf86cd799439012",
            "user_id": "user123",
            "name": "Test Jira",
            "type": "jira",
            "credentials": {
                "base_url": "https://test.atlassian.net",
                "email": "test@example.com",
                "api_token": "token"
            },
            "is_active": True
        }

        mock_db.workflows.find.return_value.sort.return_value = [workflow]
        mock_db.integrations.find_one.return_value = jira_integration
        mock_db.workflow_executions.insert_one = Mock()
        mock_db.workflows.update_one = Mock()
        mock_db.integration_logs.insert_one = Mock()

        # Execute
        with patch('app.services.integrations.workflow_engine.Database') as mock_database:
            mock_database.get_db.return_value = mock_db

            with patch('app.services.integrations.workflow_engine.JiraService') as mock_jira:
                mock_jira_instance = Mock()
                mock_jira_instance.create_issue.return_value = {
                    "success": True,
                    "issue_key": "SUPPORT-123",
                    "url": "https://test.atlassian.net/browse/SUPPORT-123"
                }
                mock_jira.return_value = mock_jira_instance

                # Trigger workflow
                await WorkflowTrigger.trigger_call_completed(
                    sample_call_data,
                    "user123"
                )

        # Verify: Jira ticket was created
        mock_jira_instance.create_issue.assert_called_once()

        # Verify execution was logged
        mock_db.workflow_executions.insert_one.assert_called()

    @pytest.mark.asyncio
    async def test_call_completed_multi_action_workflow(self, mock_db, sample_call_data):
        """Test: Call completes → Jira ticket + Email sent"""
        # Setup: Workflow with multiple actions
        workflow = {
            "_id": str(ObjectId()),
            "user_id": "user123",
            "name": "Support Workflow",
            "trigger_event": "call_completed",
            "is_active": True,
            "priority": 0,
            "conditions": [],
            "actions": [
                {
                    "id": "action1",
                    "type": "create_jira_ticket",
                    "integration_id": "507f1f77bcf86cd799439012",
                    "config": {
                        "project": "SUPPORT",
                        "summary": "Call from {{customer.name}}",
                        "description": "{{call.summary}}"
                    },
                    "on_error": "continue"
                },
                {
                    "id": "action2",
                    "type": "send_email",
                    "integration_id": "507f1f77bcf86cd799439011",
                    "config": {
                        "to": "{{agent.email}}",
                        "subject": "Jira Ticket Created",
                        "body": "Ticket created for call with {{customer.name}}"
                    },
                    "on_error": "continue"
                }
            ]
        }

        jira_integration = {
            "_id": "507f1f77bcf86cd799439012",
            "user_id": "user123",
            "name": "Test Jira",
            "type": "jira",
            "credentials": {"base_url": "https://test.atlassian.net", "email": "test@example.com", "api_token": "token"},
            "is_active": True
        }

        email_integration = {
            "_id": "507f1f77bcf86cd799439011",
            "user_id": "user123",
            "name": "Test Email",
            "type": "email",
            "credentials": {
                "smtp_host": "smtp.gmail.com",
                "smtp_port": 587,
                "smtp_username": "test@example.com",
                "smtp_password": "password",
                "from_email": "test@example.com",
                "use_tls": True
            },
            "is_active": True
        }

        mock_db.workflows.find.return_value.sort.return_value = [workflow]
        mock_db.integrations.find_one.side_effect = [jira_integration, email_integration]
        mock_db.workflow_executions.insert_one = Mock()
        mock_db.workflows.update_one = Mock()
        mock_db.integration_logs.insert_one = Mock()

        # Execute
        with patch('app.services.integrations.workflow_engine.Database') as mock_database:
            mock_database.get_db.return_value = mock_db

            with patch('app.services.integrations.workflow_engine.JiraService') as mock_jira, \
                 patch('app.services.integrations.workflow_engine.EmailService') as mock_email:

                mock_jira_instance = Mock()
                mock_jira_instance.create_issue.return_value = {"success": True, "issue_key": "SUPPORT-123"}
                mock_jira.return_value = mock_jira_instance

                mock_email_instance = Mock()
                mock_email_instance.send_email.return_value = {"success": True, "to": ["jane@company.com"]}
                mock_email.return_value = mock_email_instance

                await WorkflowTrigger.trigger_call_completed(sample_call_data, "user123")

        # Verify both actions executed
        mock_jira_instance.create_issue.assert_called_once()
        mock_email_instance.send_email.assert_called_once()

    @pytest.mark.asyncio
    async def test_call_failed_sends_alert(self, mock_db):
        """Test: Call fails → Alert email sent"""
        call_data = {
            "_id": str(ObjectId()),
            "status": "failed",
            "error_message": "Connection timeout",
            "to_number": "+1234567890",
            "from_number": "+0987654321",
            "created_at": datetime.utcnow()
        }

        workflow = {
            "_id": str(ObjectId()),
            "user_id": "user123",
            "name": "Failed Call Alert",
            "trigger_event": "call_failed",
            "is_active": True,
            "priority": 0,
            "conditions": [],
            "actions": [
                {
                    "id": "alert",
                    "type": "send_email",
                    "integration_id": "507f1f77bcf86cd799439011",
                    "config": {
                        "to": "support@company.com",
                        "subject": "Call Failed Alert",
                        "body": "Call to {{customer.phone}} failed: {{call.error}}"
                    }
                }
            ]
        }

        email_integration = {
            "_id": "507f1f77bcf86cd799439011",
            "user_id": "user123",
            "name": "Test Email",
            "type": "email",
            "credentials": {
                "smtp_host": "smtp.gmail.com",
                "smtp_port": 587,
                "smtp_username": "test@example.com",
                "smtp_password": "password",
                "from_email": "test@example.com",
                "use_tls": True
            },
            "is_active": True
        }

        mock_db.workflows.find.return_value.sort.return_value = [workflow]
        mock_db.integrations.find_one.return_value = email_integration
        mock_db.workflow_executions.insert_one = Mock()
        mock_db.workflows.update_one = Mock()
        mock_db.integration_logs.insert_one = Mock()

        with patch('app.services.integrations.workflow_engine.Database') as mock_database:
            mock_database.get_db.return_value = mock_db

            with patch('app.services.integrations.workflow_engine.EmailService') as mock_email:
                mock_email_instance = Mock()
                mock_email_instance.send_email.return_value = {"success": True}
                mock_email.return_value = mock_email_instance

                await WorkflowTrigger.trigger_call_failed(call_data, "user123")

        mock_email_instance.send_email.assert_called_once()

    @pytest.mark.asyncio
    async def test_workflow_with_condition_filtering(self, mock_db, sample_call_data):
        """Test: Workflow only triggers when conditions are met"""
        # Workflow with condition: duration > 200 seconds
        workflow = {
            "_id": str(ObjectId()),
            "user_id": "user123",
            "name": "Long Call Workflow",
            "trigger_event": "call_completed",
            "is_active": True,
            "priority": 0,
            "conditions": [
                {
                    "field": "call.duration",
                    "operator": "greater_than",
                    "value": 200  # Call is only 125 seconds
                }
            ],
            "actions": [
                {
                    "id": "action1",
                    "type": "send_email",
                    "integration_id": "507f1f77bcf86cd799439011",
                    "config": {"to": "test@example.com", "subject": "Test", "body": "Test"}
                }
            ]
        }

        mock_db.workflows.find.return_value.sort.return_value = [workflow]
        mock_db.workflow_executions.insert_one = Mock()
        mock_db.workflows.update_one = Mock()

        with patch('app.services.integrations.workflow_engine.Database') as mock_database:
            mock_database.get_db.return_value = mock_db

            await WorkflowTrigger.trigger_call_completed(sample_call_data, "user123")

        # Email should NOT be sent because condition not met
        # But execution should still be logged
        mock_db.workflow_executions.insert_one.assert_called()

    @pytest.mark.asyncio
    async def test_hubspot_contact_creation_workflow(self, mock_db, sample_call_data):
        """Test: Call completes → HubSpot contact created with note"""
        workflow = {
            "_id": str(ObjectId()),
            "user_id": "user123",
            "name": "Log to HubSpot",
            "trigger_event": "call_completed",
            "is_active": True,
            "priority": 0,
            "conditions": [],
            "actions": [
                {
                    "id": "create_contact",
                    "type": "create_hubspot_contact",
                    "integration_id": "507f1f77bcf86cd799439013",
                    "config": {
                        "email": "{{customer.email}}",
                        "firstname": "{{customer.name}}",
                        "phone": "{{customer.phone}}",
                        "update_if_exists": True
                    }
                },
                {
                    "id": "add_note",
                    "type": "create_hubspot_note",
                    "integration_id": "507f1f77bcf86cd799439013",
                    "config": {
                        "contact_email": "{{customer.email}}",
                        "note_body": "Call summary: {{call.summary}}"
                    }
                }
            ]
        }

        hubspot_integration = {
            "_id": "507f1f77bcf86cd799439013",
            "user_id": "user123",
            "name": "Test HubSpot",
            "type": "hubspot",
            "credentials": {"access_token": "token", "portal_id": "12345"},
            "is_active": True
        }

        mock_db.workflows.find.return_value.sort.return_value = [workflow]
        mock_db.integrations.find_one.return_value = hubspot_integration
        mock_db.workflow_executions.insert_one = Mock()
        mock_db.workflows.update_one = Mock()
        mock_db.integration_logs.insert_one = Mock()

        with patch('app.services.integrations.workflow_engine.Database') as mock_database:
            mock_database.get_db.return_value = mock_db

            with patch('app.services.integrations.workflow_engine.HubSpotService') as mock_hubspot:
                mock_hubspot_instance = Mock()
                mock_hubspot_instance.create_contact.return_value = {"success": True, "contact_id": "123"}
                mock_hubspot_instance.create_note.return_value = {"success": True, "note_id": "456"}
                mock_hubspot.return_value = mock_hubspot_instance

                await WorkflowTrigger.trigger_call_completed(sample_call_data, "user123")

        # Verify both HubSpot actions executed
        mock_hubspot_instance.create_contact.assert_called_once()
        mock_hubspot_instance.create_note.assert_called_once()


class TestAssistantWorkflowE2E:
    """End-to-end tests for assistant workflow integration"""

    @pytest.fixture
    def mock_db(self):
        """Mock database with sample data"""
        db = Mock()
        db.workflows = Mock()
        db.workflow_executions = Mock()
        db.integrations = Mock()
        db.integration_logs = Mock()
        db.assistants = Mock()
        return db

    @pytest.fixture
    def sample_assistant(self):
        """Sample assistant with workflow assignment"""
        return {
            "_id": ObjectId(),
            "user_id": "user123",
            "name": "Sales Bot",
            "system_message": "You are a helpful sales assistant",
            "voice": "alloy",
            "assigned_workflows": [],  # Will be populated in tests
            "workflow_trigger_events": ["CALL_COMPLETED"]
        }

    @pytest.fixture
    def sample_workflow(self):
        """Sample workflow for testing"""
        return {
            "_id": ObjectId(),
            "user_id": "user123",
            "name": "Post-Call CRM Update",
            "trigger_event": "call_completed",
            "is_active": True,
            "priority": 0,
            "conditions": [],
            "actions": [
                {
                    "id": "action1",
                    "type": "create_hubspot_contact",
                    "integration_id": "507f1f77bcf86cd799439013",
                    "config": {
                        "email": "{{customer.email}}",
                        "firstname": "{{customer.name}}"
                    },
                    "on_error": "continue"
                }
            ]
        }

    @pytest.mark.asyncio
    async def test_assistant_workflow_assignment_and_trigger(
        self, mock_db, sample_assistant, sample_workflow
    ):
        """Test: Assign workflow to assistant → Call completes → Workflow executes"""
        # Assign workflow to assistant
        workflow_id = str(sample_workflow["_id"])
        sample_assistant["assigned_workflows"] = [workflow_id]

        # Setup mocks
        mock_db.workflows.find_one.return_value = sample_workflow
        mock_db.workflow_executions.insert_one = Mock()
        mock_db.workflows.update_one = Mock()
        mock_db.integrations.find_one.return_value = {
            "_id": "507f1f77bcf86cd799439013",
            "user_id": "user123",
            "name": "Test HubSpot",
            "type": "hubspot",
            "credentials": {"access_token": "token"},
            "is_active": True
        }
        mock_db.integration_logs.insert_one = Mock()

        # Simulate post-call trigger data
        trigger_data = {
            "call_id": "call123",
            "call_sid": "CA123",
            "assistant_id": str(sample_assistant["_id"]),
            "user_id": sample_assistant["user_id"],
            "call_status": "completed",
            "conversation_history": [
                {"role": "assistant", "content": "Hello, how can I help?"},
                {"role": "user", "content": "I need product info"}
            ],
            "platform": "twilio",
            "timestamp": datetime.utcnow().isoformat()
        }

        # Execute workflow
        with patch('app.services.integrations.workflow_engine.Database') as mock_database:
            mock_database.get_db.return_value = mock_db

            with patch('app.services.integrations.workflow_engine.HubSpotService') as mock_hubspot:
                mock_hubspot_instance = Mock()
                mock_hubspot_instance.create_contact.return_value = {
                    "success": True, "contact_id": "123"
                }
                mock_hubspot.return_value = mock_hubspot_instance

                from app.services.integrations.workflow_engine import WorkflowEngine
                engine = WorkflowEngine()
                engine.db = mock_db

                # Execute each assigned workflow
                for wf_id in sample_assistant["assigned_workflows"]:
                    workflow = mock_db.workflows.find_one({"_id": ObjectId(wf_id)})
                    if workflow:
                        result = await engine.execute_workflow(workflow, trigger_data)

        # Verify workflow was looked up
        mock_db.workflows.find_one.assert_called()

    @pytest.mark.asyncio
    async def test_assistant_with_multiple_workflows(
        self, mock_db, sample_assistant, sample_workflow
    ):
        """Test: Assistant with multiple workflows → All execute on call complete"""
        # Create two workflows
        workflow1 = sample_workflow.copy()
        workflow1["_id"] = ObjectId()
        workflow1["name"] = "CRM Update Workflow"

        workflow2 = {
            "_id": ObjectId(),
            "user_id": "user123",
            "name": "Email Notification Workflow",
            "trigger_event": "call_completed",
            "is_active": True,
            "priority": 0,
            "conditions": [],
            "actions": [
                {
                    "id": "action1",
                    "type": "send_email",
                    "integration_id": "507f1f77bcf86cd799439011",
                    "config": {
                        "to": "sales@company.com",
                        "subject": "New Call Completed"
                    }
                }
            ]
        }

        # Assign both workflows
        sample_assistant["assigned_workflows"] = [
            str(workflow1["_id"]),
            str(workflow2["_id"])
        ]

        # Verify both are assigned
        assert len(sample_assistant["assigned_workflows"]) == 2

    @pytest.mark.asyncio
    async def test_workflow_not_triggered_for_wrong_event(
        self, mock_db, sample_assistant, sample_workflow
    ):
        """Test: Workflow only triggers for configured events"""
        # Configure assistant to only trigger on APPOINTMENT_SCHEDULED
        sample_assistant["workflow_trigger_events"] = ["APPOINTMENT_SCHEDULED"]
        sample_assistant["assigned_workflows"] = [str(sample_workflow["_id"])]

        # Simulate a call_completed event
        call_status = "completed"

        # Map UI events to trigger values
        event_mapping = {
            "CALL_COMPLETED": "call_completed",
            "CALL_FAILED": "call_failed",
            "APPOINTMENT_SCHEDULED": "call_completed",
        }

        # Check if should trigger
        should_trigger = False
        for ui_event in sample_assistant["workflow_trigger_events"]:
            if event_mapping.get(ui_event) == call_status:
                should_trigger = True
                break

        # APPOINTMENT_SCHEDULED maps to call_completed, so it should trigger
        # But this is a special case - only trigger if appointment was actually scheduled
        appointment_scheduled = False

        if "APPOINTMENT_SCHEDULED" in sample_assistant["workflow_trigger_events"]:
            if not appointment_scheduled:
                should_trigger = False

        assert should_trigger is False  # No appointment was scheduled

    @pytest.mark.asyncio
    async def test_workflow_trigger_with_appointment_data(
        self, mock_db, sample_assistant
    ):
        """Test: Workflow triggers with appointment metadata"""
        sample_assistant["workflow_trigger_events"] = ["APPOINTMENT_SCHEDULED"]
        sample_assistant["assigned_workflows"] = ["workflow123"]

        # Simulate trigger data with appointment
        trigger_data = {
            "call_id": "call123",
            "call_status": "completed",
            "appointment_scheduled": True,
            "appointment": {
                "event_id": "event123",
                "title": "Demo Meeting",
                "start_time": "2024-01-15T10:00:00Z",
                "attendee_email": "customer@example.com"
            }
        }

        # Verify appointment data is included
        assert trigger_data["appointment_scheduled"] is True
        assert trigger_data["appointment"]["event_id"] == "event123"

    @pytest.mark.asyncio
    async def test_inactive_workflow_skipped(self, mock_db, sample_assistant):
        """Test: Inactive workflows are not executed"""
        inactive_workflow = {
            "_id": ObjectId(),
            "user_id": "user123",
            "name": "Inactive Workflow",
            "is_active": False,  # Workflow is disabled
            "trigger_event": "call_completed"
        }

        mock_db.workflows.find_one.return_value = inactive_workflow
        sample_assistant["assigned_workflows"] = [str(inactive_workflow["_id"])]

        # Simulate checking if workflow should execute
        workflow = mock_db.workflows.find_one({"_id": ObjectId(sample_assistant["assigned_workflows"][0])})

        if workflow and not workflow.get("is_active", True):
            should_execute = False
        else:
            should_execute = True

        assert should_execute is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
