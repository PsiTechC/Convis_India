"""
Integration System Tests
Tests for credential encryption, integration CRUD, and workflow integration
"""
import pytest
import sys
import os
from datetime import datetime
from unittest.mock import Mock, patch, MagicMock
from bson import ObjectId

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.integrations.credentials_encryption import CredentialsEncryption, credentials_encryption
from app.models.integration import (
    Integration, IntegrationType, IntegrationStatus,
    JiraCredentials, HubSpotCredentials, EmailCredentials,
    SlackCredentials, CREDENTIALS_MODEL_MAP
)


class TestCredentialsEncryption:
    """Test suite for credentials encryption service"""

    def setup_method(self):
        """Setup test fixtures"""
        # Reset singleton for fresh tests
        CredentialsEncryption._instance = None
        CredentialsEncryption._fernet = None
        self.encryption = CredentialsEncryption()
        self.test_user_id = "test_user_123"

    def test_encrypt_decrypt_basic(self):
        """Test basic string encryption/decryption"""
        original = "my-secret-api-key"
        encrypted = self.encryption.encrypt(original)
        decrypted = self.encryption.decrypt(encrypted)

        assert encrypted != original, "Encrypted should differ from original"
        assert decrypted == original, "Decrypted should match original"

    def test_encrypt_decrypt_with_user_salt(self):
        """Test encryption with user-specific salt"""
        original = "my-secret-api-key"
        encrypted = self.encryption.encrypt(original, self.test_user_id)
        decrypted = self.encryption.decrypt(encrypted, self.test_user_id)

        assert decrypted == original, "Decrypted should match original with salt"

    def test_user_salt_isolation(self):
        """Test that different users get different encryptions"""
        original = "same-api-key"
        encrypted_user1 = self.encryption.encrypt(original, "user1")
        encrypted_user2 = self.encryption.encrypt(original, "user2")

        # Same data, different users = different encrypted values
        assert encrypted_user1 != encrypted_user2

    def test_encrypt_credentials_dict(self):
        """Test encrypting a credentials dictionary"""
        credentials = {
            "base_url": "https://company.atlassian.net",
            "email": "user@company.com",
            "api_token": "secret-token-123",
            "default_project": "PROJ"
        }

        encrypted = self.encryption.encrypt_credentials(credentials, self.test_user_id)

        # Non-sensitive fields should remain unchanged
        assert encrypted["base_url"] == credentials["base_url"]
        assert encrypted["email"] == credentials["email"]
        assert encrypted["default_project"] == credentials["default_project"]

        # Sensitive field should be encrypted
        assert encrypted["api_token"]["_encrypted"] == True
        assert "value" in encrypted["api_token"]
        assert encrypted["api_token"]["value"] != credentials["api_token"]

    def test_decrypt_credentials_dict(self):
        """Test decrypting a credentials dictionary"""
        original_credentials = {
            "base_url": "https://company.atlassian.net",
            "email": "user@company.com",
            "api_token": "secret-token-123",
            "default_project": "PROJ"
        }

        # Encrypt then decrypt
        encrypted = self.encryption.encrypt_credentials(original_credentials, self.test_user_id)
        decrypted = self.encryption.decrypt_credentials(encrypted, self.test_user_id)

        assert decrypted == original_credentials

    def test_encrypt_hubspot_credentials(self):
        """Test encrypting HubSpot credentials"""
        credentials = {
            "access_token": "pat-na1-xxxxx",
            "portal_id": "12345678"
        }

        encrypted = self.encryption.encrypt_credentials(credentials, self.test_user_id)

        assert encrypted["portal_id"] == credentials["portal_id"]
        assert encrypted["access_token"]["_encrypted"] == True

        decrypted = self.encryption.decrypt_credentials(encrypted, self.test_user_id)
        assert decrypted == credentials

    def test_encrypt_email_credentials(self):
        """Test encrypting Email/SMTP credentials"""
        credentials = {
            "smtp_host": "smtp.gmail.com",
            "smtp_port": 587,
            "smtp_username": "user@gmail.com",
            "smtp_password": "app-password-123",
            "from_email": "user@gmail.com",
            "use_tls": True
        }

        encrypted = self.encryption.encrypt_credentials(credentials, self.test_user_id)

        # Non-sensitive fields unchanged
        assert encrypted["smtp_host"] == credentials["smtp_host"]
        assert encrypted["smtp_port"] == credentials["smtp_port"]
        assert encrypted["from_email"] == credentials["from_email"]

        # Password encrypted
        assert encrypted["smtp_password"]["_encrypted"] == True

        decrypted = self.encryption.decrypt_credentials(encrypted, self.test_user_id)
        assert decrypted == credentials

    def test_mask_credentials(self):
        """Test masking credentials for display"""
        credentials = {
            "base_url": "https://company.atlassian.net",
            "api_token": "secret-token-123456"
        }

        masked = self.encryption.mask_credentials(credentials)

        assert masked["base_url"] == credentials["base_url"]
        assert masked["api_token"].startswith("••••")
        assert masked["api_token"].endswith("3456")


class TestIntegrationModels:
    """Test integration Pydantic models"""

    def test_jira_credentials_model(self):
        """Test JiraCredentials model validation"""
        creds = JiraCredentials(
            base_url="https://company.atlassian.net",
            email="user@company.com",
            api_token="my-api-token"
        )

        assert creds.base_url == "https://company.atlassian.net"
        assert creds.email == "user@company.com"
        assert creds.default_issue_type == "Task"  # Default value

    def test_jira_credentials_missing_required(self):
        """Test JiraCredentials fails with missing required fields"""
        with pytest.raises(Exception):
            JiraCredentials(
                base_url="https://company.atlassian.net"
                # Missing email and api_token
            )

    def test_hubspot_credentials_model(self):
        """Test HubSpotCredentials model validation"""
        creds = HubSpotCredentials(
            access_token="pat-na1-xxxxx"
        )

        assert creds.access_token == "pat-na1-xxxxx"
        assert creds.portal_id is None  # Optional

    def test_email_credentials_model(self):
        """Test EmailCredentials model validation"""
        creds = EmailCredentials(
            smtp_host="smtp.gmail.com",
            smtp_port=587,
            smtp_username="user@gmail.com",
            smtp_password="password123",
            from_email="user@gmail.com"
        )

        assert creds.smtp_port == 587
        assert creds.use_tls == True  # Default

    def test_slack_credentials_model(self):
        """Test SlackCredentials model validation"""
        creds = SlackCredentials(
            webhook_url="https://hooks.slack.com/services/xxx/yyy/zzz"
        )

        assert "hooks.slack.com" in creds.webhook_url

    def test_integration_model(self):
        """Test Integration model"""
        integration = Integration(
            user_id="user123",
            name="My Jira",
            type=IntegrationType.JIRA,
            credentials={"masked": True},
            status=IntegrationStatus.ACTIVE
        )

        assert integration.user_id == "user123"
        assert integration.type == IntegrationType.JIRA
        assert integration.status == IntegrationStatus.ACTIVE

    def test_credentials_model_map(self):
        """Test CREDENTIALS_MODEL_MAP has correct mappings"""
        assert CREDENTIALS_MODEL_MAP[IntegrationType.JIRA] == JiraCredentials
        assert CREDENTIALS_MODEL_MAP[IntegrationType.HUBSPOT] == HubSpotCredentials
        assert CREDENTIALS_MODEL_MAP[IntegrationType.EMAIL] == EmailCredentials
        assert CREDENTIALS_MODEL_MAP[IntegrationType.SLACK] == SlackCredentials


class TestJiraService:
    """Test Jira service integration"""

    def setup_method(self):
        """Setup test fixtures"""
        self.creds = JiraCredentials(
            base_url="https://test.atlassian.net",
            email="test@test.com",
            api_token="test-token-123",
            default_project="TEST"
        )

    @patch('app.services.integrations.jira_service.requests.request')
    def test_jira_test_connection_success(self, mock_request):
        """Test successful Jira connection test"""
        from app.services.integrations.jira_service import JiraService

        mock_response = Mock()
        mock_response.json.return_value = {
            "displayName": "Test User",
            "emailAddress": "test@test.com"
        }
        mock_response.text = '{"displayName": "Test User"}'
        mock_response.raise_for_status = Mock()
        mock_request.return_value = mock_response

        service = JiraService(self.creds)
        result = service.test_connection()

        assert result["success"] == True
        assert "Test User" in result["user"]

    @patch('app.services.integrations.jira_service.requests.request')
    def test_jira_test_connection_failure(self, mock_request):
        """Test failed Jira connection test"""
        from app.services.integrations.jira_service import JiraService
        import requests

        mock_response = Mock()
        mock_response.text = '{"errorMessages": ["Unauthorized"]}'
        mock_response.raise_for_status.side_effect = requests.exceptions.HTTPError(
            response=mock_response
        )
        mock_request.return_value = mock_response

        service = JiraService(self.creds)
        result = service.test_connection()

        assert result["success"] == False
        assert "troubleshooting" in result

    @patch('app.services.integrations.jira_service.requests.request')
    def test_jira_create_issue(self, mock_request):
        """Test Jira issue creation"""
        from app.services.integrations.jira_service import JiraService

        mock_response = Mock()
        mock_response.json.return_value = {
            "id": "10001",
            "key": "TEST-123"
        }
        mock_response.text = '{"id": "10001", "key": "TEST-123"}'
        mock_response.raise_for_status = Mock()
        mock_request.return_value = mock_response

        service = JiraService(self.creds)
        result = service.create_issue(
            config={
                "project": "TEST",
                "summary": "Test issue from {{caller_name}}",
                "description": "Issue description"
            },
            context_data={"caller_name": "John Doe"}
        )

        assert result["success"] == True
        assert result["issue_key"] == "TEST-123"


class TestIntegrationRoutes:
    """Test integration API routes"""

    def setup_method(self):
        """Setup test fixtures"""
        self.mock_user = {
            "_id": ObjectId("507f1f77bcf86cd799439011"),
            "email": "test@test.com"
        }
        self.mock_integration_data = {
            "name": "My Jira",
            "type": "jira",
            "credentials": {
                "base_url": "https://test.atlassian.net",
                "email": "test@test.com",
                "api_token": "test-token"
            }
        }

    def test_integration_id_to_objectid_conversion(self):
        """Test that string IDs are properly converted to ObjectId"""
        test_id = "507f1f77bcf86cd799439011"
        obj_id = ObjectId(test_id)

        assert str(obj_id) == test_id
        assert isinstance(obj_id, ObjectId)

    def test_invalid_objectid_raises_error(self):
        """Test that invalid IDs raise proper errors"""
        with pytest.raises(Exception):
            ObjectId("invalid-id")

    def test_integration_dict_to_model(self):
        """Test converting MongoDB doc to Integration model"""
        mongo_doc = {
            "_id": "507f1f77bcf86cd799439011",  # String, as we convert it
            "user_id": "user123",
            "name": "Test Integration",
            "type": "jira",
            "credentials": {"masked": True},
            "status": "active",
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
            "is_active": True
        }

        integration = Integration(**mongo_doc)
        assert integration.name == "Test Integration"
        assert integration.type == IntegrationType.JIRA


class TestWorkflowIntegration:
    """Test workflow engine integration with credentials system"""

    def setup_method(self):
        """Setup test fixtures"""
        self.encryption = credentials_encryption
        self.test_user_id = "workflow_user_123"

    def test_workflow_can_decrypt_credentials(self):
        """Test that workflow engine can decrypt stored credentials"""
        # Simulate stored encrypted credentials
        original_creds = {
            "base_url": "https://company.atlassian.net",
            "email": "user@company.com",
            "api_token": "secret-token-for-workflow"
        }

        # Encrypt (as done during storage)
        encrypted = self.encryption.encrypt_credentials(original_creds, self.test_user_id)

        # Decrypt (as done during workflow execution)
        decrypted = self.encryption.decrypt_credentials(encrypted, self.test_user_id)

        # Verify workflow can use the credentials
        assert decrypted["api_token"] == original_creds["api_token"]
        assert decrypted["base_url"] == original_creds["base_url"]

    def test_workflow_context_template_rendering(self):
        """Test template rendering with call context data"""
        from app.services.integrations.template_renderer import TemplateRenderer

        call_data = {
            "caller_phone": "+1234567890",
            "caller_name": "John Doe",
            "call_summary": "User reported login issues",
            "issue_category": "Technical Support",
            "customer_email": "john@example.com"
        }

        # Test Jira summary template
        template = "Support ticket from {{caller_name}}: {{issue_category}}"
        rendered = TemplateRenderer.render(template, call_data)

        assert rendered == "Support ticket from John Doe: Technical Support"

    def test_workflow_context_nested_data(self):
        """Test template rendering with nested context data"""
        from app.services.integrations.template_renderer import TemplateRenderer

        context = {
            "call": {
                "duration": 120,
                "agent": "Bot-1"
            },
            "customer": {
                "name": "Jane Doe",
                "email": "jane@example.com"
            }
        }

        template = "Call handled by {{call.agent}} for {{customer.name}}"
        rendered = TemplateRenderer.render(template, context)

        assert "Bot-1" in rendered
        assert "Jane Doe" in rendered


class TestEndToEndFlow:
    """End-to-end integration tests"""

    def setup_method(self):
        """Setup test fixtures"""
        self.encryption = credentials_encryption
        self.test_user_id = "e2e_user_123"

    def test_full_jira_integration_flow(self):
        """Test complete flow: create integration -> store encrypted -> decrypt -> use"""
        # 1. User provides credentials via UI
        user_credentials = {
            "base_url": "https://mycompany.atlassian.net",
            "email": "dev@mycompany.com",
            "api_token": "ATATT3xFfGF0abc123xyz",
            "default_project": "SUPPORT"
        }

        # 2. Validate with Pydantic model
        validated = JiraCredentials(**user_credentials)
        creds_dict = validated.dict()

        # 3. Encrypt for storage
        encrypted = self.encryption.encrypt_credentials(creds_dict, self.test_user_id)

        # Verify encryption
        assert encrypted["api_token"]["_encrypted"] == True
        assert encrypted["base_url"] == user_credentials["base_url"]

        # 4. Simulate storing in MongoDB
        stored_integration = {
            "_id": str(ObjectId()),
            "user_id": self.test_user_id,
            "name": "Production Jira",
            "type": "jira",
            "credentials": encrypted,
            "status": "testing",
            "created_at": datetime.utcnow()
        }

        # 5. Retrieve and decrypt for use
        retrieved_creds = self.encryption.decrypt_credentials(
            stored_integration["credentials"],
            self.test_user_id
        )

        # 6. Verify we can create service with decrypted credentials
        from app.services.integrations.jira_service import JiraService
        service_creds = JiraCredentials(**retrieved_creds)
        service = JiraService(service_creds)

        assert service.base_url == "https://mycompany.atlassian.net"
        assert service.email == "dev@mycompany.com"
        assert service.api_token == "ATATT3xFfGF0abc123xyz"

    def test_full_workflow_execution_with_integration(self):
        """Test workflow execution using stored integration credentials"""
        # Setup: Create and encrypt integration
        jira_creds = {
            "base_url": "https://company.atlassian.net",
            "email": "bot@company.com",
            "api_token": "workflow-api-token",
            "default_project": "CALLS"
        }

        encrypted_creds = self.encryption.encrypt_credentials(jira_creds, self.test_user_id)

        # Simulate call completion data
        call_context = {
            "call_id": "call_abc123",
            "caller_phone": "+15551234567",
            "caller_name": "Alice Smith",
            "call_duration": 180,
            "call_summary": "Customer reported website loading slowly",
            "issue_description": "Website performance issues - pages taking 10+ seconds to load",
            "issue_category": "Performance",
            "issue_priority": "High",
            "customer_email": "alice@customer.com",
            "action_items": ["Check server logs", "Review recent deployments"]
        }

        # Workflow would:
        # 1. Decrypt credentials
        decrypted_creds = self.encryption.decrypt_credentials(encrypted_creds, self.test_user_id)

        # 2. Create issue config with templates
        issue_config = {
            "project": "CALLS",
            "summary": "Support: {{issue_category}} - {{caller_name}}",
            "description": "Issue: {{issue_description}}\n\nCaller: {{caller_name}}\nEmail: {{customer_email}}",
            "priority": "{{issue_priority}}"
        }

        # 3. Render templates
        from app.services.integrations.template_renderer import TemplateRenderer

        rendered_summary = TemplateRenderer.render(issue_config["summary"], call_context)
        rendered_desc = TemplateRenderer.render(issue_config["description"], call_context)

        assert rendered_summary == "Support: Performance - Alice Smith"
        assert "Website performance issues" in rendered_desc
        assert "alice@customer.com" in rendered_desc


def run_tests():
    """Run all tests and print results"""
    print("=" * 60)
    print("INTEGRATION SYSTEM TEST SUITE")
    print("=" * 60)

    # Run with pytest
    exit_code = pytest.main([
        __file__,
        "-v",
        "--tb=short",
        "-x"  # Stop on first failure
    ])

    return exit_code


if __name__ == "__main__":
    run_tests()
