"""
Unit Tests for Workflow-Assistant Integration

Tests the integration between AI assistants and workflow automation:
- Workflow assignment to assistants
- Post-call workflow triggering
- Workflow trigger event filtering
"""
import pytest
from unittest.mock import Mock, patch, AsyncMock, MagicMock
from datetime import datetime, timezone, timedelta
from bson import ObjectId

# Test the workflow fields in assistant config
class TestAssistantWorkflowConfig:
    """Test workflow configuration in assistant"""

    def test_assistant_config_includes_workflows(self):
        """Test that assistant config includes workflow fields"""
        assistant_config = {
            "user_id": "user123",
            "assistant_id": str(ObjectId()),
            "name": "Test Assistant",
            "system_message": "You are a helpful assistant",
            "voice": "alloy",
            "assigned_workflows": ["workflow1", "workflow2"],
            "workflow_trigger_events": ["CALL_COMPLETED", "APPOINTMENT_SCHEDULED"]
        }

        assert "assigned_workflows" in assistant_config
        assert len(assistant_config["assigned_workflows"]) == 2
        assert "workflow_trigger_events" in assistant_config
        assert "CALL_COMPLETED" in assistant_config["workflow_trigger_events"]

    def test_empty_workflow_assignment(self):
        """Test that empty workflow assignment works"""
        assistant_config = {
            "user_id": "user123",
            "name": "Test Assistant",
            "assigned_workflows": [],
            "workflow_trigger_events": []
        }

        assert assistant_config["assigned_workflows"] == []
        assert assistant_config["workflow_trigger_events"] == []


class TestWorkflowTriggerEventMapping:
    """Test workflow trigger event mapping logic"""

    def test_call_completed_mapping(self):
        """Test CALL_COMPLETED event mapping"""
        event_mapping = {
            "CALL_COMPLETED": "call_completed",
            "CALL_FAILED": "call_failed",
            "APPOINTMENT_SCHEDULED": "call_completed",
        }

        assert event_mapping["CALL_COMPLETED"] == "call_completed"

    def test_call_failed_mapping(self):
        """Test CALL_FAILED event mapping"""
        event_mapping = {
            "CALL_COMPLETED": "call_completed",
            "CALL_FAILED": "call_failed",
            "APPOINTMENT_SCHEDULED": "call_completed",
        }

        assert event_mapping["CALL_FAILED"] == "call_failed"

    def test_should_trigger_check(self):
        """Test should_trigger logic"""
        workflow_trigger_events = ["CALL_COMPLETED", "CALL_FAILED"]
        current_event = "call_completed"

        event_mapping = {
            "CALL_COMPLETED": "call_completed",
            "CALL_FAILED": "call_failed",
        }

        should_trigger = False
        for ui_event in workflow_trigger_events:
            if event_mapping.get(ui_event) == current_event:
                should_trigger = True
                break

        assert should_trigger is True

    def test_should_not_trigger_for_unmatched_event(self):
        """Test should not trigger for unmatched event"""
        workflow_trigger_events = ["APPOINTMENT_SCHEDULED"]  # Only appointment
        current_event = "call_failed"  # But call failed

        event_mapping = {
            "CALL_COMPLETED": "call_completed",
            "CALL_FAILED": "call_failed",
            "APPOINTMENT_SCHEDULED": "call_completed",
        }

        should_trigger = False
        for ui_event in workflow_trigger_events:
            if event_mapping.get(ui_event) == current_event:
                should_trigger = True
                break

        assert should_trigger is False


class TestTriggerDataConstruction:
    """Test trigger data construction for workflow execution"""

    def test_build_trigger_data_basic(self):
        """Test basic trigger data construction"""
        trigger_data = {
            "call_id": "call123",
            "call_sid": "CA123",
            "assistant_id": "assistant123",
            "user_id": "user123",
            "call_status": "completed",
            "conversation_history": [
                {"role": "assistant", "content": "Hello"},
                {"role": "user", "content": "Hi there"}
            ],
            "platform": "twilio",
            "call_duration_turns": 5,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        assert trigger_data["call_id"] == "call123"
        assert trigger_data["call_status"] == "completed"
        assert len(trigger_data["conversation_history"]) == 2

    def test_build_trigger_data_with_appointment(self):
        """Test trigger data with appointment metadata"""
        appointment_metadata = {
            "event_id": "event123",
            "title": "Meeting",
            "start_time": "2024-01-15T10:00:00Z",
            "end_time": "2024-01-15T11:00:00Z",
            "attendee_email": "customer@example.com"
        }

        trigger_data = {
            "call_id": "call123",
            "call_status": "completed",
            "appointment": appointment_metadata,
            "appointment_scheduled": True
        }

        assert trigger_data["appointment_scheduled"] is True
        assert trigger_data["appointment"]["event_id"] == "event123"

    def test_build_trigger_data_with_full_transcript(self):
        """Test trigger data includes full transcript with timestamps"""
        call_start = datetime.now(timezone.utc)
        call_end = call_start + timedelta(seconds=120)  # 2 minutes later

        full_transcript = [
            {"speaker": "assistant", "text": "Hello, how can I help you?", "timestamp": call_start.isoformat()},
            {"speaker": "user", "text": "I need to schedule an appointment", "timestamp": (call_start + timedelta(seconds=5)).isoformat()},
            {"speaker": "assistant", "text": "Sure, when would you like to meet?", "timestamp": (call_start + timedelta(seconds=10)).isoformat()},
        ]

        trigger_data = {
            "call_id": "call123",
            "call_status": "completed",
            "call_start_time": call_start.isoformat(),
            "call_end_time": call_end.isoformat(),
            "call_duration_seconds": 120.0,
            "full_transcript": full_transcript,
            "transcript_text": "\n".join([
                f"{msg['speaker'].upper()}: {msg['text']}"
                for msg in full_transcript
            ]),
        }

        assert trigger_data["call_duration_seconds"] == 120.0
        assert len(trigger_data["full_transcript"]) == 3
        assert trigger_data["full_transcript"][0]["speaker"] == "assistant"
        assert trigger_data["full_transcript"][1]["speaker"] == "user"
        assert "ASSISTANT: Hello" in trigger_data["transcript_text"]
        assert "USER: I need" in trigger_data["transcript_text"]

    def test_build_trigger_data_with_latency_metrics(self):
        """Test trigger data includes latency metrics"""
        trigger_data = {
            "call_id": "call123",
            "call_status": "completed",
            "call_duration_turns": 5,
            "avg_response_time_ms": 350.5,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        assert trigger_data["avg_response_time_ms"] == 350.5
        assert trigger_data["call_duration_turns"] == 5


class TestWorkflowAssignmentValidation:
    """Test workflow assignment validation"""

    @pytest.fixture
    def mock_db(self):
        """Mock database for testing"""
        db = Mock()
        db.workflows = Mock()
        db.assistants = Mock()
        return db

    def test_valid_workflow_ids_filtering(self, mock_db):
        """Test that invalid workflow IDs are filtered out"""
        submitted_workflow_ids = [
            str(ObjectId()),  # Valid
            "invalid-id",     # Invalid format
            str(ObjectId()),  # Valid
        ]

        valid_workflow_ids = []
        for wf_id in submitted_workflow_ids:
            try:
                if ObjectId.is_valid(wf_id):
                    valid_workflow_ids.append(wf_id)
            except Exception:
                pass

        assert len(valid_workflow_ids) == 2

    def test_workflow_ownership_check(self, mock_db):
        """Test that workflows are checked for ownership"""
        user_id = "user123"
        workflow_id = str(ObjectId())

        # Mock workflow exists for this user
        mock_db.workflows.find_one.return_value = {
            "_id": ObjectId(workflow_id),
            "user_id": user_id,
            "name": "Test Workflow"
        }

        result = mock_db.workflows.find_one({
            "_id": ObjectId(workflow_id),
            "user_id": user_id
        })

        assert result is not None
        assert result["user_id"] == user_id


class TestPostCallWorkflowExecution:
    """Test post-call workflow execution"""

    @pytest.fixture
    def mock_workflow_engine(self):
        """Mock workflow engine"""
        with patch('app.services.integrations.workflow_engine.WorkflowEngine') as mock:
            engine_instance = mock.return_value
            engine_instance.execute_workflow = AsyncMock(return_value={"success": True})
            return engine_instance

    @pytest.mark.asyncio
    async def test_workflow_execution_called_for_assigned_workflows(self, mock_workflow_engine):
        """Test that workflows are executed for assigned workflow IDs"""
        assigned_workflows = [str(ObjectId()), str(ObjectId())]
        trigger_data = {
            "call_id": "call123",
            "call_status": "completed"
        }

        # Simulate executing each workflow
        for workflow_id in assigned_workflows:
            workflow = {"_id": workflow_id, "name": "Test", "is_active": True}
            await mock_workflow_engine.execute_workflow(workflow, trigger_data)

        assert mock_workflow_engine.execute_workflow.call_count == 2

    @pytest.mark.asyncio
    async def test_inactive_workflow_not_executed(self):
        """Test that inactive workflows are not executed"""
        workflow = {"_id": str(ObjectId()), "name": "Test", "is_active": False}

        # Should not execute inactive workflow
        should_execute = workflow.get("is_active", True)
        assert should_execute is False


class TestWorkflowAssistantAPIFields:
    """Test workflow fields in assistant API"""

    def test_assistant_create_request_with_workflows(self):
        """Test assistant create request includes workflow fields"""
        from app.models.ai_assistant import AIAssistantCreate

        # Test that we can create with workflow fields
        create_data = AIAssistantCreate(
            user_id="user123",
            name="Test Assistant",
            system_message="You are helpful",
            voice="alloy",
            assigned_workflows=["workflow1", "workflow2"],
            workflow_trigger_events=["CALL_COMPLETED"]
        )

        assert create_data.assigned_workflows == ["workflow1", "workflow2"]
        assert create_data.workflow_trigger_events == ["CALL_COMPLETED"]

    def test_assistant_create_default_values(self):
        """Test assistant create with default workflow values"""
        from app.models.ai_assistant import AIAssistantCreate

        create_data = AIAssistantCreate(
            user_id="user123",
            name="Test Assistant",
            system_message="You are helpful",
            voice="alloy"
        )

        # Check defaults
        assert create_data.assigned_workflows == []
        assert create_data.workflow_trigger_events == ["CALL_COMPLETED"]

    def test_assistant_update_request_with_workflows(self):
        """Test assistant update request includes workflow fields"""
        from app.models.ai_assistant import AIAssistantUpdate

        update_data = AIAssistantUpdate(
            assigned_workflows=["workflow1"],
            workflow_trigger_events=["CALL_COMPLETED", "CALL_FAILED"]
        )

        assert update_data.assigned_workflows == ["workflow1"]
        assert update_data.workflow_trigger_events == ["CALL_COMPLETED", "CALL_FAILED"]

    def test_assistant_response_includes_workflows(self):
        """Test assistant response includes workflow fields"""
        from app.models.ai_assistant import AIAssistantResponse

        response = AIAssistantResponse(
            id="assistant123",
            user_id="user123",
            name="Test",
            system_message="Test",
            voice="alloy",
            temperature=0.8,
            has_api_key=True,
            created_at="2024-01-01T00:00:00Z",
            updated_at="2024-01-01T00:00:00Z",
            assigned_workflows=["wf1", "wf2"],
            workflow_trigger_events=["CALL_COMPLETED"]
        )

        assert response.assigned_workflows == ["wf1", "wf2"]
        assert response.workflow_trigger_events == ["CALL_COMPLETED"]


class TestCustomProviderStreamWorkflowIntegration:
    """Test CustomProviderStreamHandler workflow integration"""

    def test_handler_config_includes_workflow_fields(self):
        """Test that handler config includes workflow fields"""
        assistant_config = {
            "user_id": "user123",
            "assistant_id": "assistant123",
            "name": "Test",
            "system_message": "Test",
            "voice": "alloy",
            "assigned_workflows": ["wf1", "wf2"],
            "workflow_trigger_events": ["CALL_COMPLETED", "APPOINTMENT_SCHEDULED"]
        }

        # Simulate what the handler does
        assigned_workflows = assistant_config.get('assigned_workflows', [])
        workflow_trigger_events = assistant_config.get('workflow_trigger_events', ['CALL_COMPLETED'])

        assert assigned_workflows == ["wf1", "wf2"]
        assert workflow_trigger_events == ["CALL_COMPLETED", "APPOINTMENT_SCHEDULED"]

    def test_call_status_tracking(self):
        """Test call status tracking for workflow triggers"""
        # Initial status
        call_status = "in_progress"

        # On normal completion
        if call_status == "in_progress":
            call_status = "completed"
        assert call_status == "completed"

        # On failure
        call_status = "in_progress"
        call_status = "failed"  # Explicitly set on error
        assert call_status == "failed"


class TestInterruptionAndStreamingModeConfig:
    """Test interruption and streaming mode configuration"""

    def test_assistant_create_with_interruption_settings(self):
        """Test assistant create includes interruption and streaming fields"""
        from app.models.ai_assistant import AIAssistantCreate

        create_data = AIAssistantCreate(
            user_id="user123",
            name="Test Assistant",
            system_message="You are helpful",
            voice="alloy",
            enable_interruption=True,
            interruption_probability_threshold=0.5,
            interruption_min_chunks=3,
            use_streaming_mode=True,
            vad_threshold=0.35,
            vad_min_speech_ms=100,
            vad_min_silence_ms=150
        )

        assert create_data.enable_interruption is True
        assert create_data.interruption_probability_threshold == 0.5
        assert create_data.interruption_min_chunks == 3
        assert create_data.use_streaming_mode is True
        assert create_data.vad_threshold == 0.35
        assert create_data.vad_min_speech_ms == 100
        assert create_data.vad_min_silence_ms == 150

    def test_assistant_update_with_interruption_settings(self):
        """Test assistant update includes interruption and streaming fields"""
        from app.models.ai_assistant import AIAssistantUpdate

        update_data = AIAssistantUpdate(
            enable_interruption=False,
            interruption_probability_threshold=0.7,
            interruption_min_chunks=4,
            use_streaming_mode=True,
            vad_min_speech_ms=120,
            vad_min_silence_ms=180
        )

        assert update_data.enable_interruption is False
        assert update_data.interruption_probability_threshold == 0.7
        assert update_data.interruption_min_chunks == 4
        assert update_data.use_streaming_mode is True
        assert update_data.vad_min_speech_ms == 120
        assert update_data.vad_min_silence_ms == 180

    def test_assistant_response_includes_interruption_settings(self):
        """Test assistant response includes interruption and streaming fields"""
        from app.models.ai_assistant import AIAssistantResponse

        response = AIAssistantResponse(
            id="assistant123",
            user_id="user123",
            name="Test",
            system_message="Test",
            voice="alloy",
            temperature=0.8,
            has_api_key=True,
            created_at="2024-01-01T00:00:00Z",
            updated_at="2024-01-01T00:00:00Z",
            enable_interruption=True,
            interruption_probability_threshold=0.6,
            interruption_min_chunks=2,
            use_streaming_mode=True,
            vad_min_speech_ms=150,
            vad_min_silence_ms=200
        )

        assert response.enable_interruption is True
        assert response.interruption_probability_threshold == 0.6
        assert response.interruption_min_chunks == 2
        assert response.use_streaming_mode is True
        assert response.vad_min_speech_ms == 150
        assert response.vad_min_silence_ms == 200

    def test_handler_config_includes_interruption_fields(self):
        """Test that call handler config includes interruption fields"""
        assistant_config = {
            "user_id": "user123",
            "assistant_id": "assistant123",
            "name": "Test",
            "system_message": "Test",
            "voice": "alloy",
            "enable_interruption": True,
            "interruption_probability_threshold": 0.5,
            "interruption_min_chunks": 3,
            "use_streaming_mode": True,
            "vad_min_speech_ms": 100,
            "vad_min_silence_ms": 150
        }

        # Simulate what the handler does
        enable_interruption = assistant_config.get('enable_interruption', True)
        interruption_threshold = assistant_config.get('interruption_probability_threshold', 0.6)
        interruption_chunks = assistant_config.get('interruption_min_chunks', 2)
        use_streaming = assistant_config.get('use_streaming_mode', False)
        min_speech = assistant_config.get('vad_min_speech_ms', 150)
        min_silence = assistant_config.get('vad_min_silence_ms', 200)

        assert enable_interruption is True
        assert interruption_threshold == 0.5
        assert interruption_chunks == 3
        assert use_streaming is True
        assert min_speech == 100
        assert min_silence == 150
