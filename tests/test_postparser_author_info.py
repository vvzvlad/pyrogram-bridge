# flake8: noqa
# pylint: disable=broad-exception-raised, raise-missing-from, too-many-arguments, redefined-outer-name
# pylint: disable=multiple-statements, logging-fstring-interpolation, trailing-whitespace, line-too-long
# pylint: disable=broad-exception-caught, missing-function-docstring, missing-class-docstring
# pylint: disable=f-string-without-interpolation, protected-access, wrong-import-position
# pylance: disable=reportMissingImports, reportMissingModuleSource

import unittest
from unittest.mock import MagicMock
import sys
import os
# Add project root to sys.path to find post_parser
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# Mock the config module
sys.modules['config'] = __import__('tests.mock_config', fromlist=['get_settings'])

from pyrogram.types import Message
from post_parser import PostParser

class TestPostParserGetAuthorInfo(unittest.TestCase):
    def setUp(self):
        self.client_mock = MagicMock()
        self.parser = PostParser(self.client_mock)

    def _mock_message(self, sender_chat=None, from_user=None):
        message = MagicMock(spec=Message)
        message.sender_chat = sender_chat
        message.from_user = from_user
        return message

    def test_sender_chat_with_username(self):
        sender_chat = MagicMock()
        sender_chat.title = 'Test Channel'
        sender_chat.username = 'testchannel'
        message = self._mock_message(sender_chat=sender_chat)
        self.assertEqual(self.parser._get_author_info(message), 'Test Channel (@testchannel)')

    def test_sender_chat_without_username(self):
        sender_chat = MagicMock()
        sender_chat.title = 'No Username Channel'
        sender_chat.username = None
        message = self._mock_message(sender_chat=sender_chat)
        self.assertEqual(self.parser._get_author_info(message), 'No Username Channel')

    def test_sender_chat_empty_title(self):
        sender_chat = MagicMock()
        sender_chat.title = None
        sender_chat.username = 'testchannel'
        message = self._mock_message(sender_chat=sender_chat)
        self.assertEqual(self.parser._get_author_info(message), '@testchannel')

    def test_from_user_with_username(self):
        from_user = MagicMock()
        from_user.first_name = 'John'
        from_user.last_name = 'Doe'
        from_user.username = 'johndoe'
        message = self._mock_message(from_user=from_user)
        self.assertEqual(self.parser._get_author_info(message), 'John Doe (@johndoe)')

    def test_from_user_without_username(self):
        from_user = MagicMock()
        from_user.first_name = 'Jane'
        from_user.last_name = 'Smith'
        from_user.username = None
        message = self._mock_message(from_user=from_user)
        self.assertEqual(self.parser._get_author_info(message), 'Jane Smith')

    def test_from_user_only_first_name(self):
        from_user = MagicMock()
        from_user.first_name = 'Alice'
        from_user.last_name = None
        from_user.username = None
        message = self._mock_message(from_user=from_user)
        self.assertEqual(self.parser._get_author_info(message), 'Alice')

    def test_from_user_only_last_name(self):
        from_user = MagicMock()
        from_user.first_name = None
        from_user.last_name = 'Brown'
        from_user.username = None
        message = self._mock_message(from_user=from_user)
        self.assertEqual(self.parser._get_author_info(message), 'Brown')

    def test_from_user_empty_names(self):
        from_user = MagicMock()
        from_user.first_name = None
        from_user.last_name = None
        from_user.username = None
        message = self._mock_message(from_user=from_user)
        self.assertEqual(self.parser._get_author_info(message), 'Unknown author')

    def test_no_sender_chat_no_from_user(self):
        message = self._mock_message(sender_chat=None, from_user=None)
        self.assertEqual(self.parser._get_author_info(message), 'Unknown author')

if __name__ == '__main__':
    unittest.main() 
