"""
Integration Tests for Tool Calling During Voice Calls

Tests the complete flow:
1. Assistant with tools configured
2. Call starts
3. AI decides to call a tool
4. Tool executes and returns result
5. AI continues conversation with tool result
6. Workflow triggered after tool execution (if configured)
"""

import pytest
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock
from datetime import datetime

from app.services.realtime_tool_service import RealtimeToolService
from app.models.ai_assistant import AIAssistantCreate, AIAssistantUpdate


class TestAssistantToolConfiguration:
    """Test assistant model with tools configuration"""

    def test_create_assistant_with_tools(self):
        """Test creating an assistant with tools enabled"""
        assistant = AIAssistantCreate(
            user_id="user123",
            name="Test Assistant",
            system_message="You are a helpful assistant",
            tools_enabled=True,
            tools=[
                {
                    "name": "lookup_customer",
                    "type": "webhook",
                    "description": "Look up customer info",
                    "server": {"url": "https://api.example.com/customer"}
                }
            ],
            max_tool_calls_per_turn=5,
            tool_execution_timeout=30
        )

        assert assistant.tools_enabled is True
        assert len(assistant.tools) == 1
        assert assistant.tools[0]["name"] == "lookup_customer"
        assert assistant.max_tool_calls_per_turn == 5
        assert assistant.tool_execution_timeout == 30

    def test_create_assistant_without_tools(self):
        """Test creating an assistant without tools (default)"""
        assistant = AIAssistantCreate(
            user_id="user123",
            name="Test Assistant",
            system_message="You are a helpful assistant"
        )

        assert assistant.tools_enabled is False
        assert assistant.tools == []

    def test_update_assistant_enable_tools(self):
        """Test updating an assistant to enable tools"""
        update = AIAssistantUpdate(
            tools_enabled=True,
            tools=[
                {
                    "name": "check_availability",
                    "type": "webhook",
                    "description": "Check appointment slots"
                }
            ]
        )

        assert update.tools_enabled is True
        assert len(update.tools) == 1


class TestCallHandlerToolIntegration:
    """Test tool calling within call handler context"""

    @pytest.fixture
    def mock_assistant_config(self):
        """Create a mock assistant config with tools enabled"""
        return {
            "assistant_id": "test_assistant_123",
            "user_id": "test_user_456",
            "system_message": "You are a helpful assistant with tools",
            "voice": "alloy",
            "temperature": 0.8,
            "greeting": "Hello! How can I help you?",
            "asr_provider": "openai",
            "tts_provider": "openai",
            "llm_provider": "openai",
            "llm_model": "gpt-4o-mini",
            "llm_max_tokens": 150,
            "tools_enabled": True,
            "tools": [
                {
                    "name": "get_customer_info",
                    "type": "webhook",
                    "description": "Look up customer information by phone number",
                    "server": {
                        "url": "https://api.example.com/customers/lookup"
                    },
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "phone": {"type": "string", "description": "Phone number"}
                        },
                        "required": ["phone"]
                    }
                },
                {
                    "name": "create_ticket",
                    "type": "webhook",
                    "description": "Create a support ticket",
                    "server": {
                        "url": "https://api.example.com/tickets"
                    },
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string"},
                            "description": {"type": "string"}
                        },
                        "required": ["title"]
                    }
                }
            ],
            "max_tool_calls_per_turn": 5,
            "tool_execution_timeout": 30,
            "provider_keys": {"openai": "test_key"}
        }

    def test_tool_service_initialization_from_config(self, mock_assistant_config):
        """Test that RealtimeToolService initializes correctly from assistant config"""
        service = RealtimeToolService(
            user_id=mock_assistant_config["user_id"],
            assistant_id=mock_assistant_config["assistant_id"],
            call_id="test_call_789"
        )

        # Generate schema for tools
        schema = service.get_openai_tools_schema(mock_assistant_config["tools"])

        assert len(schema) == 2
        assert schema[0]["function"]["name"] == "get_customer_info"
        assert schema[1]["function"]["name"] == "create_ticket"

    @pytest.mark.asyncio
    async def test_tool_execution_during_call_simulation(self, mock_assistant_config):
        """Simulate a complete tool execution during a call"""
        service = RealtimeToolService(
            user_id=mock_assistant_config["user_id"],
            assistant_id=mock_assistant_config["assistant_id"],
            call_id="test_call_789"
        )

        # Mock webhook response
        with patch('aiohttp.ClientSession') as mock_session:
            mock_response = AsyncMock()
            mock_response.status = 200
            mock_response.text = AsyncMock(return_value=json.dumps({
                "results": [{
                    "result": {
                        "customer_name": "John Doe",
                        "email": "john@example.com",
                        "account_status": "active"
                    }
                }]
            }))

            mock_context = AsyncMock()
            mock_context.__aenter__ = AsyncMock(return_value=mock_response)
            mock_context.__aexit__ = AsyncMock(return_value=None)

            mock_session_instance = AsyncMock()
            mock_session_instance.request = MagicMock(return_value=mock_context)
            mock_session_instance.__aenter__ = AsyncMock(return_value=mock_session_instance)
            mock_session_instance.__aexit__ = AsyncMock(return_value=None)

            mock_session.return_value = mock_session_instance

            # Execute the tool as the AI would during a call
            result = await service.execute_tool(
                tool_name="get_customer_info",
                tool_arguments={"phone": "+1234567890"},
                tool_configs=mock_assistant_config["tools"],
                context={
                    "call_id": "test_call_789",
                    "conversation_history": [
                        {"role": "user", "content": "Can you look up my account?"}
                    ]
                }
            )

        assert result["success"] is True
        assert "data" in result

        # Verify execution was logged
        log = service.get_execution_log()
        assert len(log) == 1
        assert log[0]["tool_name"] == "get_customer_info"


class TestLLMToolCallingFlow:
    """Test the complete LLM tool calling flow"""

    @pytest.fixture
    def tool_configs(self):
        return [
            {
                "name": "get_order_status",
                "type": "function",
                "description": "Get the status of a customer's order",
                "server": {"url": "https://api.example.com/orders"},
                "parameters": {
                    "type": "object",
                    "properties": {
                        "order_id": {"type": "string", "description": "The order ID"}
                    },
                    "required": ["order_id"]
                }
            }
        ]

    @pytest.mark.asyncio
    async def test_openai_tool_schema_format(self, tool_configs):
        """Test that tool schema matches OpenAI expected format"""
        service = RealtimeToolService()
        schema = service.get_openai_tools_schema(tool_configs)

        # OpenAI expects this exact structure
        expected_keys = {"type", "function"}
        assert set(schema[0].keys()) == expected_keys

        function_keys = {"name", "description", "parameters"}
        assert set(schema[0]["function"].keys()) == function_keys

    @pytest.mark.asyncio
    async def test_tool_result_format_for_llm(self, tool_configs):
        """Test that tool results are formatted correctly for LLM consumption"""
        service = RealtimeToolService(call_id="test")

        with patch('aiohttp.ClientSession') as mock_session:
            mock_response = AsyncMock()
            mock_response.status = 200
            mock_response.text = AsyncMock(return_value='{"order_status": "shipped", "tracking": "ABC123"}')

            mock_context = AsyncMock()
            mock_context.__aenter__ = AsyncMock(return_value=mock_response)
            mock_context.__aexit__ = AsyncMock(return_value=None)

            mock_session_instance = AsyncMock()
            mock_session_instance.request = MagicMock(return_value=mock_context)
            mock_session_instance.__aenter__ = AsyncMock(return_value=mock_session_instance)
            mock_session_instance.__aexit__ = AsyncMock(return_value=None)

            mock_session.return_value = mock_session_instance

            result = await service.execute_tool(
                tool_name="get_order_status",
                tool_arguments={"order_id": "ORD-12345"},
                tool_configs=tool_configs,
                context={}
            )

        # Result should be JSON serializable for LLM message
        result_json = json.dumps(result)
        assert isinstance(result_json, str)


class TestToolCallingWithWorkflows:
    """Test that tool calls can trigger workflows"""

    @pytest.fixture
    def workflow_tool_configs(self):
        """Tools that should trigger workflow actions"""
        return [
            {
                "name": "create_support_ticket",
                "type": "webhook",
                "description": "Create a support ticket for the customer's issue",
                "server": {"url": "https://api.example.com/jira/create"},
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "description": {"type": "string"},
                        "priority": {"type": "string", "enum": ["low", "medium", "high"]}
                    },
                    "required": ["title"]
                },
                # This tool's execution could trigger a workflow
                "trigger_workflow": True,
                "workflow_event": "TICKET_CREATED"
            }
        ]

    @pytest.mark.asyncio
    async def test_tool_execution_logs_for_workflow(self, workflow_tool_configs):
        """Test that tool execution creates logs that workflows can use"""
        service = RealtimeToolService(
            call_id="test_call",
            user_id="user123",
            assistant_id="assistant456"
        )

        with patch('aiohttp.ClientSession') as mock_session:
            mock_response = AsyncMock()
            mock_response.status = 201
            mock_response.text = AsyncMock(return_value='{"ticket_id": "TICKET-123", "url": "https://jira.example.com/TICKET-123"}')

            mock_context = AsyncMock()
            mock_context.__aenter__ = AsyncMock(return_value=mock_response)
            mock_context.__aexit__ = AsyncMock(return_value=None)

            mock_session_instance = AsyncMock()
            mock_session_instance.request = MagicMock(return_value=mock_context)
            mock_session_instance.__aenter__ = AsyncMock(return_value=mock_session_instance)
            mock_session_instance.__aexit__ = AsyncMock(return_value=None)

            mock_session.return_value = mock_session_instance

            result = await service.execute_tool(
                tool_name="create_support_ticket",
                tool_arguments={
                    "title": "Customer unable to login",
                    "description": "Customer reports login issues since yesterday",
                    "priority": "high"
                },
                tool_configs=workflow_tool_configs,
                context={"call_id": "test_call"}
            )

        # Get the execution log - this can be used by workflows
        log = service.get_execution_log()

        assert len(log) == 1
        assert log[0]["tool_name"] == "create_support_ticket"
        assert log[0]["arguments"]["title"] == "Customer unable to login"
        assert log[0]["arguments"]["priority"] == "high"
        assert "execution_time_ms" in log[0]

        # This log data can be passed to workflow engine for post-call processing


class TestErrorHandling:
    """Test error handling in tool calling"""

    @pytest.mark.asyncio
    async def test_webhook_timeout_handling(self):
        """Test handling of webhook timeout"""
        service = RealtimeToolService(call_id="test")

        tool_configs = [
            {
                "name": "slow_api",
                "type": "webhook",
                "url": "https://api.example.com/slow",
                "timeout": 1  # 1 second timeout
            }
        ]

        with patch('aiohttp.ClientSession') as mock_session:
            mock_session_instance = AsyncMock()

            # Simulate timeout
            async def timeout_request(*args, **kwargs):
                raise asyncio.TimeoutError()

            mock_context = AsyncMock()
            mock_context.__aenter__ = AsyncMock(side_effect=asyncio.TimeoutError())

            mock_session_instance.request = MagicMock(return_value=mock_context)
            mock_session_instance.__aenter__ = AsyncMock(return_value=mock_session_instance)
            mock_session_instance.__aexit__ = AsyncMock(return_value=None)

            mock_session.return_value = mock_session_instance

            result = await service.execute_tool(
                tool_name="slow_api",
                tool_arguments={},
                tool_configs=tool_configs,
                context={}
            )

        assert result["success"] is False
        assert "timed out" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_webhook_http_error_handling(self):
        """Test handling of HTTP errors"""
        service = RealtimeToolService(call_id="test")

        tool_configs = [
            {
                "name": "failing_api",
                "type": "webhook",
                "url": "https://api.example.com/fail"
            }
        ]

        with patch('aiohttp.ClientSession') as mock_session:
            mock_response = AsyncMock()
            mock_response.status = 500
            mock_response.text = AsyncMock(return_value='{"error": "Internal Server Error"}')

            mock_context = AsyncMock()
            mock_context.__aenter__ = AsyncMock(return_value=mock_response)
            mock_context.__aexit__ = AsyncMock(return_value=None)

            mock_session_instance = AsyncMock()
            mock_session_instance.request = MagicMock(return_value=mock_context)
            mock_session_instance.__aenter__ = AsyncMock(return_value=mock_session_instance)
            mock_session_instance.__aexit__ = AsyncMock(return_value=None)

            mock_session.return_value = mock_session_instance

            result = await service.execute_tool(
                tool_name="failing_api",
                tool_arguments={},
                tool_configs=tool_configs,
                context={}
            )

        assert result["success"] is False
        assert result["status_code"] == 500

    @pytest.mark.asyncio
    async def test_invalid_tool_arguments_handling(self):
        """Test handling of invalid tool arguments"""
        service = RealtimeToolService(call_id="test")

        tool_configs = [
            {
                "name": "test_tool",
                "type": "function"
            }
        ]

        # Execute with invalid arguments - should not crash
        result = await service.execute_tool(
            tool_name="test_tool",
            tool_arguments=None,  # Invalid
            tool_configs=tool_configs,
            context={}
        )

        # Should handle gracefully
        assert "result" in result or "success" in result


class TestConcurrentToolCalls:
    """Test concurrent tool execution"""

    @pytest.mark.asyncio
    async def test_multiple_concurrent_tools(self):
        """Test executing multiple tools concurrently"""
        service = RealtimeToolService(call_id="test")

        tool_configs = [
            {"name": "tool1", "type": "function"},
            {"name": "tool2", "type": "function"},
            {"name": "tool3", "type": "function"},
        ]

        # Execute tools concurrently
        tasks = [
            service.execute_tool(f"tool{i}", {"arg": i}, tool_configs, {})
            for i in range(1, 4)
        ]

        results = await asyncio.gather(*tasks)

        assert len(results) == 3

        # All executions should be logged
        log = service.get_execution_log()
        assert len(log) == 3


# Run tests
if __name__ == "__main__":
    pytest.main([__file__, "-v"])
