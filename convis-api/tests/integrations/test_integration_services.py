"""
Unit Tests for Integration Services (Jira, HubSpot, Email)
Uses mocks to avoid actual API calls
"""
import pytest
from unittest.mock import Mock, patch, MagicMock
from app.services.integrations.jira_service import JiraService
from app.services.integrations.hubspot_service import HubSpotService
from app.services.integrations.email_service import EmailService
from app.models.integration import JiraCredentials, HubSpotCredentials, EmailCredentials


class TestJiraService:
    """Test suite for Jira integration service"""

    @pytest.fixture
    def jira_credentials(self):
        """Fixture for Jira credentials"""
        return JiraCredentials(
            base_url="https://test.atlassian.net",
            email="test@example.com",
            api_token="test-token",
            default_project="TEST",
            default_issue_type="Task"
        )

    @pytest.fixture
    def jira_service(self, jira_credentials):
        """Fixture for Jira service"""
        return JiraService(jira_credentials)

    @patch('app.services.integrations.jira_service.requests.request')
    def test_test_connection_success(self, mock_request, jira_service):
        """Test successful Jira connection"""
        mock_response = Mock()
        mock_response.json.return_value = {"displayName": "Test User"}
        mock_response.text = '{"displayName": "Test User"}'
        mock_request.return_value = mock_response

        result = jira_service.test_connection()

        assert result["success"] is True
        assert "Test User" in result["message"] or "user" in result

    @patch('app.services.integrations.jira_service.requests.request')
    def test_create_issue_success(self, mock_request, jira_service):
        """Test creating Jira issue"""
        mock_response = Mock()
        mock_response.json.return_value = {
            "key": "TEST-123",
            "id": "10001"
        }
        mock_response.text = '{"key": "TEST-123"}'
        mock_response.raise_for_status = Mock()
        mock_request.return_value = mock_response

        config = {
            "project": "TEST",
            "issue_type": "Task",
            "summary": "Test Issue",
            "description": "Test Description"
        }
        context_data = {}

        result = jira_service.create_issue(config, context_data)

        assert result["success"] is True
        assert result["issue_key"] == "TEST-123"
        assert "TEST-123" in result["url"]

    @patch('app.services.integrations.jira_service.requests.request')
    def test_create_issue_with_template_variables(self, mock_request, jira_service):
        """Test creating Jira issue with template variables"""
        mock_response = Mock()
        mock_response.json.return_value = {
            "key": "TEST-456",
            "id": "10002"
        }
        mock_response.text = '{"key": "TEST-456"}'
        mock_response.raise_for_status = Mock()
        mock_request.return_value = mock_response

        config = {
            "project": "TEST",
            "issue_type": "Task",
            "summary": "Call from {{customer.name}}",
            "description": "Duration: {{call.duration}}"
        }
        context_data = {
            "customer": {"name": "John Doe"},
            "call": {"duration": 120}
        }

        result = jira_service.create_issue(config, context_data)

        assert result["success"] is True
        # Verify template was rendered
        call_args = mock_request.call_args
        request_data = call_args.kwargs.get('json') or call_args[1].get('json')
        assert "John Doe" in str(request_data)

    @patch('app.services.integrations.jira_service.requests.request')
    def test_create_issue_failure(self, mock_request, jira_service):
        """Test Jira issue creation failure"""
        mock_request.side_effect = Exception("API Error")

        config = {
            "project": "TEST",
            "summary": "Test"
        }
        context_data = {}

        result = jira_service.create_issue(config, context_data)

        assert result["success"] is False
        assert "error" in result


class TestHubSpotService:
    """Test suite for HubSpot integration service"""

    @pytest.fixture
    def hubspot_credentials(self):
        """Fixture for HubSpot credentials"""
        return HubSpotCredentials(
            access_token="test-access-token",
            portal_id="12345"
        )

    @pytest.fixture
    def hubspot_service(self, hubspot_credentials):
        """Fixture for HubSpot service"""
        return HubSpotService(hubspot_credentials)

    @patch('app.services.integrations.hubspot_service.requests.request')
    def test_test_connection_success(self, mock_request, hubspot_service):
        """Test successful HubSpot connection"""
        mock_response = Mock()
        mock_response.json.return_value = {
            "hub_id": "12345",
            "user": "test@example.com"
        }
        mock_response.text = '{"hub_id": "12345"}'
        mock_request.return_value = mock_response

        result = hubspot_service.test_connection()

        assert result["success"] is True

    @patch('app.services.integrations.hubspot_service.requests.request')
    def test_create_contact_success(self, mock_request, hubspot_service):
        """Test creating HubSpot contact"""
        # Mock search response (no existing contact)
        search_response = Mock()
        search_response.json.return_value = {"results": []}
        search_response.text = '{"results": []}'
        search_response.raise_for_status = Mock()

        # Mock create response
        create_response = Mock()
        create_response.json.return_value = {"id": "123"}
        create_response.text = '{"id": "123"}'
        create_response.raise_for_status = Mock()

        mock_request.side_effect = [search_response, create_response]

        config = {
            "email": "test@example.com",
            "firstname": "John",
            "lastname": "Doe"
        }
        context_data = {}

        result = hubspot_service.create_contact(config, context_data)

        assert result["success"] is True
        assert result["contact_id"] == "123"

    @patch('app.services.integrations.hubspot_service.requests.request')
    def test_create_contact_with_variables(self, mock_request, hubspot_service):
        """Test creating contact with template variables"""
        search_response = Mock()
        search_response.json.return_value = {"results": []}
        search_response.text = '{"results": []}'
        search_response.raise_for_status = Mock()

        create_response = Mock()
        create_response.json.return_value = {"id": "456"}
        create_response.text = '{"id": "456"}'
        create_response.raise_for_status = Mock()

        mock_request.side_effect = [search_response, create_response]

        config = {
            "email": "{{customer.email}}",
            "firstname": "{{customer.name}}"
        }
        context_data = {
            "customer": {
                "email": "john@example.com",
                "name": "John Doe"
            }
        }

        result = hubspot_service.create_contact(config, context_data)

        assert result["success"] is True

    @patch('app.services.integrations.hubspot_service.requests.request')
    def test_create_note_success(self, mock_request, hubspot_service):
        """Test creating HubSpot note"""
        # Mock search for contact
        search_response = Mock()
        search_response.json.return_value = {
            "results": [{"id": "789"}]
        }
        search_response.text = '{"results": [{"id": "789"}]}'
        search_response.raise_for_status = Mock()

        # Mock create note
        note_response = Mock()
        note_response.json.return_value = {"id": "note-123"}
        note_response.text = '{"id": "note-123"}'
        note_response.raise_for_status = Mock()

        # Mock association
        assoc_response = Mock()
        assoc_response.json.return_value = {}
        assoc_response.text = '{}'
        assoc_response.raise_for_status = Mock()

        mock_request.side_effect = [search_response, note_response, assoc_response]

        config = {
            "contact_email": "test@example.com",
            "note_body": "Test note content"
        }
        context_data = {}

        result = hubspot_service.create_note(config, context_data)

        assert result["success"] is True
        assert result["note_id"] == "note-123"


class TestEmailService:
    """Test suite for Email integration service"""

    @pytest.fixture
    def email_credentials(self):
        """Fixture for Email credentials"""
        return EmailCredentials(
            smtp_host="smtp.gmail.com",
            smtp_port=587,
            smtp_username="test@example.com",
            smtp_password="test-password",
            from_email="test@example.com",
            from_name="Test Sender",
            use_tls=True
        )

    @pytest.fixture
    def email_service(self, email_credentials):
        """Fixture for Email service"""
        return EmailService(email_credentials)

    @patch('app.services.integrations.email_service.smtplib.SMTP')
    def test_test_connection_success(self, mock_smtp, email_service):
        """Test successful email connection"""
        mock_server = MagicMock()
        mock_smtp.return_value = mock_server

        result = email_service.test_connection()

        assert result["success"] is True
        mock_server.starttls.assert_called_once()
        mock_server.login.assert_called_once()
        mock_server.quit.assert_called_once()

    @patch('app.services.integrations.email_service.smtplib.SMTP')
    def test_send_email_success(self, mock_smtp, email_service):
        """Test sending email successfully"""
        mock_server = MagicMock()
        mock_smtp.return_value = mock_server

        config = {
            "to": "recipient@example.com",
            "subject": "Test Subject",
            "body": "Test body content"
        }
        context_data = {}

        result = email_service.send_email(config, context_data)

        assert result["success"] is True
        assert "recipient@example.com" in result["to"]
        mock_server.sendmail.assert_called_once()

    @patch('app.services.integrations.email_service.smtplib.SMTP')
    def test_send_email_with_template_variables(self, mock_smtp, email_service):
        """Test sending email with template variables"""
        mock_server = MagicMock()
        mock_smtp.return_value = mock_server

        config = {
            "to": "{{customer.email}}",
            "subject": "Call with {{customer.name}}",
            "body": "Duration: {{call.duration}} seconds"
        }
        context_data = {
            "customer": {"email": "john@example.com", "name": "John"},
            "call": {"duration": 120}
        }

        result = email_service.send_email(config, context_data)

        assert result["success"] is True
        assert "john@example.com" in result["to"]
        assert "Call with John" == result["subject"]

    @patch('app.services.integrations.email_service.smtplib.SMTP')
    def test_send_email_with_multiple_recipients(self, mock_smtp, email_service):
        """Test sending email to multiple recipients"""
        mock_server = MagicMock()
        mock_smtp.return_value = mock_server

        config = {
            "to": "user1@example.com, user2@example.com",
            "subject": "Test",
            "body": "Test content"
        }
        context_data = {}

        result = email_service.send_email(config, context_data)

        assert result["success"] is True
        assert len(result["to"]) == 2

    @patch('app.services.integrations.email_service.smtplib.SMTP')
    def test_send_email_with_cc_bcc(self, mock_smtp, email_service):
        """Test sending email with CC and BCC"""
        mock_server = MagicMock()
        mock_smtp.return_value = mock_server

        config = {
            "to": "primary@example.com",
            "cc": "cc@example.com",
            "bcc": "bcc@example.com",
            "subject": "Test",
            "body": "Test content"
        }
        context_data = {}

        result = email_service.send_email(config, context_data)

        assert result["success"] is True
        # Verify sendmail was called with all recipients
        call_args = mock_server.sendmail.call_args
        recipients = call_args[0][1]
        assert "primary@example.com" in recipients
        assert "cc@example.com" in recipients
        assert "bcc@example.com" in recipients

    @patch('app.services.integrations.email_service.smtplib.SMTP')
    def test_send_email_html_format(self, mock_smtp, email_service):
        """Test sending HTML email"""
        mock_server = MagicMock()
        mock_smtp.return_value = mock_server

        config = {
            "to": "test@example.com",
            "subject": "HTML Test",
            "body": "<h1>Hello</h1><p>Test</p>",
            "format": "html"
        }
        context_data = {}

        result = email_service.send_email(config, context_data)

        assert result["success"] is True

    @patch('app.services.integrations.email_service.smtplib.SMTP')
    def test_send_email_failure(self, mock_smtp, email_service):
        """Test email sending failure"""
        mock_smtp.side_effect = Exception("SMTP Error")

        config = {
            "to": "test@example.com",
            "subject": "Test",
            "body": "Test"
        }
        context_data = {}

        result = email_service.send_email(config, context_data)

        assert result["success"] is False
        assert "error" in result

    def test_html_to_plain_conversion(self, email_service):
        """Test HTML to plain text conversion"""
        html = "<h1>Title</h1><p>Paragraph</p><br/><strong>Bold</strong>"

        plain = email_service._html_to_plain(html)

        assert "Title" in plain
        assert "Paragraph" in plain
        assert "<h1>" not in plain
        assert "<p>" not in plain


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
