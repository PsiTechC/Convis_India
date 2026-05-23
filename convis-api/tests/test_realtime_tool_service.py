"""
Unit and Integration Tests for Real-time Tool Service (Vapi-like functionality)

Tests cover:
1. Tool schema generation for OpenAI function calling
2. Webhook tool execution
3. Database query tool execution
4. Tool execution logging
5. Error handling
6. Integration with call handlers
"""

import pytest
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime

from app.services.realtime_tool_service import RealtimeToolService, DEFAULT_TOOLS


class TestRealtimeToolServiceSchema:
    """Test OpenAI tool schema generation"""

    def test_get_openai_tools_schema_function_type(self):
        """Test schema generation for function type tools"""
        service = RealtimeToolService()

        tool_configs = [
            {
                "name": "get_customer_info",
                "type": "function",
                "description": "Look up customer by phone",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "phone": {"type": "string", "description": "Customer phone"}
                    },
                    "required": ["phone"]
                }
            }
        ]

        schema = service.get_openai_tools_schema(tool_configs)

        assert len(schema) == 1
        assert schema[0]["type"] == "function"
        assert schema[0]["function"]["name"] == "get_customer_info"
        assert schema[0]["function"]["description"] == "Look up customer by phone"
        assert "phone" in schema[0]["function"]["parameters"]["properties"]

    def test_get_openai_tools_schema_webhook_type(self):
        """Test schema generation for webhook type tools"""
        service = RealtimeToolService()

        tool_configs = [
            {
                "name": "call_api",
                "type": "webhook",
                "description": "Call external API",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "data": {"type": "string"}
                    }
                }
            }
        ]

        schema = service.get_openai_tools_schema(tool_configs)

        assert len(schema) == 1
        assert schema[0]["type"] == "function"
        assert schema[0]["function"]["name"] == "call_api"

    def test_get_openai_tools_schema_multiple_tools(self):
        """Test schema generation for multiple tools"""
        service = RealtimeToolService()

        tool_configs = [
            {"name": "tool1", "type": "function", "description": "Tool 1"},
            {"name": "tool2", "type": "webhook", "description": "Tool 2"},
            {"name": "tool3", "type": "database_query", "description": "Tool 3"},
        ]

        schema = service.get_openai_tools_schema(tool_configs)

        assert len(schema) == 3

    def test_default_tools_schema(self):
        """Test that DEFAULT_TOOLS can be converted to schema"""
        service = RealtimeToolService()

        schema = service.get_openai_tools_schema(DEFAULT_TOOLS)

        # Schema should have at least one tool
        assert len(schema) >= 1
        for tool in schema:
            assert tool["type"] == "function"
            assert "name" in tool["function"]
            assert "description" in tool["function"]


class TestRealtimeToolServiceExecution:
    """Test tool execution"""

    @pytest.fixture
    def service(self):
        return RealtimeToolService(
            user_id="test_user",
            assistant_id="test_assistant",
            call_id="test_call_123"
        )

    @pytest.mark.asyncio
    async def test_execute_tool_not_found(self, service):
        """Test execution of non-existent tool"""
        result = await service.execute_tool(
            tool_name="nonexistent_tool",
            tool_arguments={},
            tool_configs=[],
            context={}
        )

        assert result["success"] is False
        assert "not found" in result["error"]

    @pytest.mark.asyncio
    async def test_execute_webhook_tool_success(self, service):
        """Test successful webhook execution"""
        tool_configs = [
            {
                "name": "test_webhook",
                "type": "webhook",
                "url": "https://api.example.com/test",
                "method": "POST"
            }
        ]

        # Mock aiohttp
        with patch('aiohttp.ClientSession') as mock_session:
            mock_response = AsyncMock()
            mock_response.status = 200
            mock_response.text = AsyncMock(return_value='{"result": "success"}')

            mock_context = AsyncMock()
            mock_context.__aenter__ = AsyncMock(return_value=mock_response)
            mock_context.__aexit__ = AsyncMock(return_value=None)

            mock_session_instance = AsyncMock()
            mock_session_instance.request = MagicMock(return_value=mock_context)
            mock_session_instance.__aenter__ = AsyncMock(return_value=mock_session_instance)
            mock_session_instance.__aexit__ = AsyncMock(return_value=None)

            mock_session.return_value = mock_session_instance

            result = await service.execute_tool(
                tool_name="test_webhook",
                tool_arguments={"key": "value"},
                tool_configs=tool_configs,
                context={}
            )

            assert result["success"] is True
            assert result["status_code"] == 200

    @pytest.mark.asyncio
    async def test_execute_webhook_no_url(self, service):
        """Test webhook execution without URL"""
        tool_configs = [
            {
                "name": "test_webhook",
                "type": "webhook"
                # No URL
            }
        ]

        result = await service.execute_tool(
            tool_name="test_webhook",
            tool_arguments={},
            tool_configs=tool_configs,
            context={}
        )

        assert result["success"] is False
        assert "URL" in result["error"] or "url" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_execution_log(self, service):
        """Test that tool executions are logged"""
        tool_configs = [
            {
                "name": "test_tool",
                "type": "function"
            }
        ]

        await service.execute_tool(
            tool_name="test_tool",
            tool_arguments={"arg": "value"},
            tool_configs=tool_configs,
            context={}
        )

        log = service.get_execution_log()

        assert len(log) == 1
        assert log[0]["tool_name"] == "test_tool"
        assert log[0]["arguments"] == {"arg": "value"}
        assert "execution_time_ms" in log[0]
        assert "timestamp" in log[0]


class TestRealtimeToolServiceDatabaseQuery:
    """Test database query tool execution"""

    @pytest.fixture
    def mock_db(self):
        """Create a mock database with proper async behavior"""
        mock_collection = MagicMock()

        # Create a proper async cursor mock
        class MockCursor:
            def __init__(self, data):
                self.data = data

            def limit(self, n):
                return self

            async def to_list(self, length):
                return self.data

        mock_cursor = MockCursor([
            {"_id": "123", "name": "Test Customer", "phone": "+1234567890"}
        ])
        mock_collection.find = MagicMock(return_value=mock_cursor)
        mock_collection.find_one = AsyncMock(return_value={"_id": "123", "name": "Test"})
        mock_collection.count_documents = AsyncMock(return_value=5)

        mock_db = MagicMock()
        mock_db.__getitem__ = MagicMock(return_value=mock_collection)
        return mock_db

    @pytest.mark.asyncio
    async def test_database_query_find(self, mock_db):
        """Test database find query"""
        service = RealtimeToolService(db=mock_db)

        tool_configs = [
            {
                "name": "query_customers",
                "type": "database_query",
                "collection": "customers"
            }
        ]

        result = await service.execute_tool(
            tool_name="query_customers",
            tool_arguments={
                "query_type": "find",
                "filters": {"phone": "+1234567890"}
            },
            tool_configs=tool_configs,
            context={}
        )

        assert result["success"] is True
        assert "data" in result

    @pytest.mark.asyncio
    async def test_database_query_count(self, mock_db):
        """Test database count query"""
        service = RealtimeToolService(db=mock_db)

        tool_configs = [
            {
                "name": "count_customers",
                "type": "database_query",
                "collection": "customers"
            }
        ]

        result = await service.execute_tool(
            tool_name="count_customers",
            tool_arguments={
                "query_type": "count",
                "filters": {}
            },
            tool_configs=tool_configs,
            context={}
        )

        assert result["success"] is True
        assert result["count"] == 5

    @pytest.mark.asyncio
    async def test_database_query_no_db(self):
        """Test database query without database connection"""
        service = RealtimeToolService(db=None)

        tool_configs = [
            {
                "name": "query_db",
                "type": "database_query",
                "collection": "test"
            }
        ]

        result = await service.execute_tool(
            tool_name="query_db",
            tool_arguments={"query_type": "find"},
            tool_configs=tool_configs,
            context={}
        )

        assert result["success"] is False
        assert "not available" in result["error"]


class TestRealtimeToolServiceTemplateRendering:
    """Test template rendering for tool arguments"""

    def test_render_template_string(self):
        """Test rendering template strings"""
        service = RealtimeToolService()

        template = "Hello {{name}}, your phone is {{phone}}"
        data = {"name": "John", "phone": "+1234567890"}

        result = service._render_template(template, data)

        assert result == "Hello John, your phone is +1234567890"

    def test_render_template_dict(self):
        """Test rendering template in dictionaries"""
        service = RealtimeToolService()

        template = {
            "greeting": "Hello {{name}}",
            "message": "Call ID: {{call_id}}"
        }
        data = {"name": "John", "call_id": "123"}

        result = service._render_template(template, data)

        assert result["greeting"] == "Hello John"
        assert result["message"] == "Call ID: 123"

    def test_render_template_list(self):
        """Test rendering template in lists"""
        service = RealtimeToolService()

        template = ["{{item1}}", "{{item2}}"]
        data = {"item1": "First", "item2": "Second"}

        result = service._render_template(template, data)

        assert result == ["First", "Second"]


class TestRealtimeToolIntegration:
    """Integration tests for tool calling in call handlers"""

    @pytest.mark.asyncio
    async def test_tool_service_initialization(self):
        """Test that tool service initializes correctly with all parameters"""
        service = RealtimeToolService(
            user_id="user123",
            assistant_id="assistant456",
            call_id="call789",
            db=None
        )

        assert service.user_id == "user123"
        assert service.assistant_id == "assistant456"
        assert service.call_id == "call789"
        assert service.tool_execution_log == []

    @pytest.mark.asyncio
    async def test_multiple_tool_executions(self):
        """Test executing multiple tools in sequence"""
        service = RealtimeToolService(call_id="test_call")

        tool_configs = [
            {"name": "tool1", "type": "function"},
            {"name": "tool2", "type": "function"},
        ]

        # Execute first tool
        await service.execute_tool("tool1", {"arg": "1"}, tool_configs, {})
        # Execute second tool
        await service.execute_tool("tool2", {"arg": "2"}, tool_configs, {})

        log = service.get_execution_log()
        assert len(log) == 2
        assert log[0]["tool_name"] == "tool1"
        assert log[1]["tool_name"] == "tool2"

    @pytest.mark.asyncio
    async def test_vapi_style_webhook_format(self):
        """Test that webhook payloads follow Vapi-style format"""
        service = RealtimeToolService(
            call_id="test_call",
            assistant_id="test_assistant"
        )

        tool_configs = [
            {
                "name": "test_webhook",
                "type": "webhook",
                "server": {
                    "url": "https://api.example.com/webhook",
                    "secret": "test_secret"
                }
            }
        ]

        # Capture the request body
        captured_body = None
        captured_headers = None

        async def mock_request(method, url, json, headers, timeout):
            nonlocal captured_body, captured_headers
            captured_body = json
            captured_headers = headers

            mock_response = AsyncMock()
            mock_response.status = 200
            mock_response.text = AsyncMock(return_value='{"success": true}')
            return mock_response

        with patch('aiohttp.ClientSession') as mock_session:
            mock_session_instance = AsyncMock()

            # Create proper async context manager
            mock_response = AsyncMock()
            mock_response.status = 200
            mock_response.text = AsyncMock(return_value='{"success": true}')

            mock_context = AsyncMock()
            mock_context.__aenter__ = AsyncMock(return_value=mock_response)
            mock_context.__aexit__ = AsyncMock(return_value=None)

            def capture_request(*args, **kwargs):
                nonlocal captured_body, captured_headers
                captured_body = kwargs.get('json')
                captured_headers = kwargs.get('headers')
                return mock_context

            mock_session_instance.request = capture_request
            mock_session_instance.__aenter__ = AsyncMock(return_value=mock_session_instance)
            mock_session_instance.__aexit__ = AsyncMock(return_value=None)

            mock_session.return_value = mock_session_instance

            await service.execute_tool(
                tool_name="test_webhook",
                tool_arguments={"customer_id": "123"},
                tool_configs=tool_configs,
                context={}
            )

        # Verify Vapi-style payload structure
        assert captured_body is not None
        assert "message" in captured_body
        assert captured_body["message"]["type"] == "tool-calls"
        assert "toolCallList" in captured_body["message"]

        # Verify secret header
        assert captured_headers is not None
        assert "x-vapi-secret" in captured_headers
        assert captured_headers["x-vapi-secret"] == "test_secret"


class TestToolCallingWithLLM:
    """Test the LLM tool calling integration"""

    @pytest.mark.asyncio
    async def test_tool_schema_compatible_with_openai(self):
        """Test that generated schema is valid for OpenAI API"""
        service = RealtimeToolService()

        tool_configs = [
            {
                "name": "get_weather",
                "type": "function",
                "description": "Get weather for a location",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "location": {
                            "type": "string",
                            "description": "City name"
                        },
                        "unit": {
                            "type": "string",
                            "enum": ["celsius", "fahrenheit"]
                        }
                    },
                    "required": ["location"]
                }
            }
        ]

        schema = service.get_openai_tools_schema(tool_configs)

        # Validate schema structure for OpenAI
        assert len(schema) == 1
        tool = schema[0]

        assert tool["type"] == "function"
        assert "function" in tool
        assert "name" in tool["function"]
        assert "description" in tool["function"]
        assert "parameters" in tool["function"]

        params = tool["function"]["parameters"]
        assert params["type"] == "object"
        assert "properties" in params
        assert "location" in params["properties"]


# Run tests with pytest
if __name__ == "__main__":
    pytest.main([__file__, "-v"])
