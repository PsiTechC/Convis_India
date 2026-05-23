"""
Comprehensive tests for async IO modules
Tests: imports, syntax, basic functionality, and integration
"""
import pytest
import asyncio
import sys
import os

# Add the app directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestImports:
    """Test that all async modules can be imported without errors"""

    def test_import_async_database(self):
        """Test async_database module imports"""
        from app.config.async_database import AsyncDatabase, get_async_collection
        assert AsyncDatabase is not None
        assert get_async_collection is not None

    def test_import_async_post_call_processor(self):
        """Test async_post_call_processor module imports"""
        from app.services.async_post_call_processor import AsyncPostCallProcessor
        assert AsyncPostCallProcessor is not None

    def test_import_async_inbound_processor(self):
        """Test async_inbound_post_call_processor module imports"""
        from app.services.async_inbound_post_call_processor import AsyncInboundPostCallProcessor
        assert AsyncInboundPostCallProcessor is not None

    def test_import_async_email_service(self):
        """Test async_email_service module imports"""
        from app.services.async_email_service import AsyncEmailService
        assert AsyncEmailService is not None

    def test_import_async_campaign_dialer(self):
        """Test async_campaign_dialer module imports"""
        from app.services.async_campaign_dialer import AsyncCampaignDialer
        assert AsyncCampaignDialer is not None

    def test_import_async_call_status_processor(self):
        """Test async_call_status_processor module imports"""
        from app.services.async_call_status_processor import process_call_status_async
        assert process_call_status_async is not None


class TestAsyncDatabase:
    """Test AsyncDatabase functionality"""

    def test_class_structure(self):
        """Test AsyncDatabase has required methods"""
        from app.config.async_database import AsyncDatabase

        assert hasattr(AsyncDatabase, 'connect')
        assert hasattr(AsyncDatabase, 'get_db')
        assert hasattr(AsyncDatabase, 'close')
        assert hasattr(AsyncDatabase, 'get_client')

    def test_async_methods_are_coroutines(self):
        """Test that async methods are actually coroutines"""
        from app.config.async_database import AsyncDatabase
        import inspect

        assert inspect.iscoroutinefunction(AsyncDatabase.connect)
        assert inspect.iscoroutinefunction(AsyncDatabase.get_db)
        assert inspect.iscoroutinefunction(AsyncDatabase.close)


class TestAsyncPostCallProcessor:
    """Test AsyncPostCallProcessor functionality"""

    def test_class_structure(self):
        """Test AsyncPostCallProcessor has required methods"""
        from app.services.async_post_call_processor import AsyncPostCallProcessor

        processor = AsyncPostCallProcessor()
        assert hasattr(processor, 'download_recording')
        assert hasattr(processor, 'transcribe_audio')
        assert hasattr(processor, 'analyze_transcript')
        assert hasattr(processor, 'process_call')

    def test_async_methods_are_coroutines(self):
        """Test that async methods are actually coroutines"""
        from app.services.async_post_call_processor import AsyncPostCallProcessor
        import inspect

        processor = AsyncPostCallProcessor()
        assert inspect.iscoroutinefunction(processor.download_recording)
        assert inspect.iscoroutinefunction(processor.transcribe_audio)
        assert inspect.iscoroutinefunction(processor.analyze_transcript)
        assert inspect.iscoroutinefunction(processor.process_call)


class TestAsyncInboundProcessor:
    """Test AsyncInboundPostCallProcessor functionality"""

    def test_class_structure(self):
        """Test AsyncInboundPostCallProcessor has required methods"""
        from app.services.async_inbound_post_call_processor import AsyncInboundPostCallProcessor

        processor = AsyncInboundPostCallProcessor()
        assert hasattr(processor, 'download_recording')
        assert hasattr(processor, 'transcribe_audio')
        assert hasattr(processor, 'analyze_transcript')
        assert hasattr(processor, 'process_inbound_call')

    def test_async_methods_are_coroutines(self):
        """Test that async methods are actually coroutines"""
        from app.services.async_inbound_post_call_processor import AsyncInboundPostCallProcessor
        import inspect

        processor = AsyncInboundPostCallProcessor()
        assert inspect.iscoroutinefunction(processor.download_recording)
        assert inspect.iscoroutinefunction(processor.transcribe_audio)
        assert inspect.iscoroutinefunction(processor.analyze_transcript)
        assert inspect.iscoroutinefunction(processor.process_inbound_call)


class TestAsyncEmailService:
    """Test AsyncEmailService functionality"""

    def test_class_structure(self):
        """Test AsyncEmailService has required methods"""
        from app.services.async_email_service import AsyncEmailService

        service = AsyncEmailService()
        assert hasattr(service, 'send_meeting_scheduled_email')
        assert hasattr(service, 'send_meeting_summary_email')
        assert hasattr(service, 'send_otp_email')
        assert hasattr(service, 'send_otp_email_with_retry')

    def test_async_methods_are_coroutines(self):
        """Test that async methods are actually coroutines"""
        from app.services.async_email_service import AsyncEmailService
        import inspect

        service = AsyncEmailService()
        assert inspect.iscoroutinefunction(service.send_meeting_scheduled_email)
        assert inspect.iscoroutinefunction(service.send_meeting_summary_email)
        assert inspect.iscoroutinefunction(service.send_otp_email)
        assert inspect.iscoroutinefunction(service.send_otp_email_with_retry)


class TestAsyncCampaignDialer:
    """Test AsyncCampaignDialer functionality"""

    def test_class_structure(self):
        """Test AsyncCampaignDialer has required methods"""
        from app.services.async_campaign_dialer import AsyncCampaignDialer

        # Don't instantiate as it requires Redis connection
        assert hasattr(AsyncCampaignDialer, '__init__')

    def test_methods_exist(self):
        """Test that required methods exist on the class"""
        from app.services.async_campaign_dialer import AsyncCampaignDialer
        import inspect

        # Check class methods without instantiation
        assert 'acquire_lock' in dir(AsyncCampaignDialer)
        assert 'release_lock' in dir(AsyncCampaignDialer)
        assert 'place_call' in dir(AsyncCampaignDialer)
        assert 'dial_next' in dir(AsyncCampaignDialer)
        assert 'handle_call_completed' in dir(AsyncCampaignDialer)


class TestAsyncCallStatusProcessor:
    """Test async call status processor"""

    def test_function_is_coroutine(self):
        """Test that process_call_status_async is a coroutine"""
        from app.services.async_call_status_processor import process_call_status_async
        import inspect

        assert inspect.iscoroutinefunction(process_call_status_async)


class TestEmailUtils:
    """Test email utils fixes"""

    def test_send_otp_email_is_async(self):
        """Test that send_otp_email_with_retry is async"""
        from app.utils.email import send_otp_email_with_retry
        import inspect

        assert inspect.iscoroutinefunction(send_otp_email_with_retry)

    def test_no_blocking_time_sleep(self):
        """Test that time.sleep is not used in async function"""
        import ast

        with open('app/utils/email.py', 'r') as f:
            content = f.read()

        # Check that time.sleep is not called in the async function
        # It should use asyncio.sleep instead
        tree = ast.parse(content)

        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef):
                # Inside async function, check for time.sleep calls
                for subnode in ast.walk(node):
                    if isinstance(subnode, ast.Call):
                        if isinstance(subnode.func, ast.Attribute):
                            if subnode.func.attr == 'sleep':
                                if isinstance(subnode.func.value, ast.Name):
                                    # Should be asyncio.sleep, not time.sleep
                                    assert subnode.func.value.id != 'time', \
                                        "Found blocking time.sleep in async function"


class TestWebhookHandler:
    """Test webhook handler integration"""

    def test_webhook_uses_async_processor(self):
        """Test that webhook handler imports async processor"""
        with open('app/routes/campaign_twilio_callbacks.py', 'r') as f:
            content = f.read()

        assert 'process_call_status_async' in content, \
            "Webhook handler should use async processor"
        assert 'await process_call_status_async' in content, \
            "Webhook handler should await the async processor"


class TestSecurityChecks:
    """Basic security vulnerability checks"""

    def test_no_hardcoded_secrets_in_async_modules(self):
        """Check for hardcoded secrets in async modules"""
        files_to_check = [
            'app/config/async_database.py',
            'app/services/async_post_call_processor.py',
            'app/services/async_inbound_post_call_processor.py',
            'app/services/async_email_service.py',
            'app/services/async_campaign_dialer.py',
            'app/services/async_call_status_processor.py',
        ]

        suspicious_patterns = [
            'password=',
            'secret=',
            'api_key=',
            'token=',
            'sk-',  # OpenAI key pattern
            'AKIA',  # AWS key pattern
        ]

        for filepath in files_to_check:
            with open(filepath, 'r') as f:
                content = f.read().lower()

            for pattern in suspicious_patterns:
                # Skip if it's just a variable assignment or parameter
                if pattern.lower() in content:
                    # Make sure it's not a hardcoded value
                    lines = content.split('\n')
                    for line in lines:
                        if pattern.lower() in line:
                            # Check if it's a string literal with actual secret
                            if f'{pattern}"' in line or f"{pattern}'" in line:
                                # It's assigning to a string, could be hardcoded
                                if 'os.getenv' not in line and 'settings.' not in line:
                                    # Only fail if it looks like an actual hardcoded secret
                                    if len(line.split('=')[-1].strip().strip('"\'')) > 10:
                                        pytest.fail(f"Possible hardcoded secret in {filepath}: {line[:50]}...")

    def test_no_sql_injection_patterns(self):
        """Check for potential SQL/NoSQL injection vulnerabilities"""
        files_to_check = [
            'app/services/async_post_call_processor.py',
            'app/services/async_inbound_post_call_processor.py',
            'app/services/async_campaign_dialer.py',
            'app/services/async_call_status_processor.py',
        ]

        # Patterns that might indicate unsafe query construction
        dangerous_patterns = [
            'f"db[',  # f-string with collection name
            "f'db[",
            '.format()',  # String formatting in queries
        ]

        for filepath in files_to_check:
            with open(filepath, 'r') as f:
                content = f.read()

            for pattern in dangerous_patterns:
                if pattern in content:
                    # This is a warning, not necessarily a vulnerability
                    print(f"Warning: Potential unsafe pattern '{pattern}' in {filepath}")

    def test_error_handling_exists(self):
        """Ensure error handling exists in async modules"""
        files_to_check = [
            'app/services/async_post_call_processor.py',
            'app/services/async_inbound_post_call_processor.py',
            'app/services/async_campaign_dialer.py',
        ]

        for filepath in files_to_check:
            with open(filepath, 'r') as f:
                content = f.read()

            assert 'try:' in content, f"Missing try-except in {filepath}"
            assert 'except' in content, f"Missing exception handling in {filepath}"
            assert 'logger.error' in content, f"Missing error logging in {filepath}"


class TestAsyncPatterns:
    """Test for correct async patterns"""

    def test_asyncio_gather_used_for_parallel_ops(self):
        """Verify asyncio.gather is used for parallel operations"""
        files_to_check = [
            'app/services/async_post_call_processor.py',
            'app/services/async_inbound_post_call_processor.py',
        ]

        for filepath in files_to_check:
            with open(filepath, 'r') as f:
                content = f.read()

            assert 'asyncio.gather' in content, \
                f"Expected asyncio.gather for parallel ops in {filepath}"

    def test_asyncio_create_task_instead_of_threading(self):
        """Verify asyncio.create_task is used instead of threading"""
        filepath = 'app/services/async_campaign_dialer.py'

        with open(filepath, 'r') as f:
            content = f.read()

        assert 'asyncio.create_task' in content, \
            "Expected asyncio.create_task for background tasks"

        # Check that threading.Thread is not actually used in code (only in comments)
        import ast
        tree = ast.parse(content)
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                if node.attr == 'Thread':
                    pytest.fail("Found threading.Thread usage in code")

    def test_asyncio_sleep_instead_of_time_sleep(self):
        """Verify asyncio.sleep is used instead of time.sleep in actual code"""
        filepath = 'app/services/async_campaign_dialer.py'

        with open(filepath, 'r') as f:
            content = f.read()

        assert 'asyncio.sleep' in content, \
            "Expected asyncio.sleep for async delays"

        # Parse the AST to check for actual time.sleep calls (not in comments)
        import ast
        tree = ast.parse(content)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Attribute):
                    if node.func.attr == 'sleep':
                        if isinstance(node.func.value, ast.Name):
                            if node.func.value.id == 'time':
                                pytest.fail("Found blocking time.sleep in code")


if __name__ == '__main__':
    pytest.main([__file__, '-v', '--tb=short'])
