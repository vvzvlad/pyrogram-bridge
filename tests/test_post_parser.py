import unittest
from unittest.mock import MagicMock, PropertyMock, patch
import re
import sys
import os
# Add project root to sys.path to find post_parser
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# Mock the config module
sys.modules['config'] = __import__('tests.mock_config', fromlist=['get_settings'])

from pyrogram.types import Message, Document
from pyrogram.enums import MessageMediaType
from post_parser import PostParser

class TestPostParserGenerateTitle(unittest.TestCase):

    def setUp(self):
        self.client_mock = MagicMock()
        self.parser = PostParser(self.client_mock)
        
    def tearDown(self):
        pass

    def _create_mock_message(self, media=None, text=None, caption=None, document_mime_type=None, channel_chat_created=False, web_page=None, poll=None):
        message = MagicMock(spec=Message)
        message.media = media
        message.text = text
        message.caption = caption
        message.web_page = web_page
        message.channel_chat_created = channel_chat_created
        message.poll = poll

        # Mock document properties if document exists
        if document_mime_type:
            doc_mock = MagicMock(spec=Document)
            type(doc_mock).mime_type = PropertyMock(return_value=document_mime_type)
            message.document = doc_mock
        else:
            message.document = None # Ensure document is None if no mime type provided

        return message

    def test_generate_title_media_photo(self):
        message = self._create_mock_message(media=MessageMediaType.PHOTO)
        self.assertEqual(self.parser._generate_title(message), "📷 Photo")

    def test_generate_title_media_video(self):
        message = self._create_mock_message(media=MessageMediaType.VIDEO)
        self.assertEqual(self.parser._generate_title(message), "🎥 Video")

    def test_generate_title_media_animation(self):
        message = self._create_mock_message(media=MessageMediaType.ANIMATION)
        self.assertEqual(self.parser._generate_title(message), "🎞 GIF")

    def test_generate_title_media_audio(self):
        message = self._create_mock_message(media=MessageMediaType.AUDIO)
        self.assertEqual(self.parser._generate_title(message), "🎵 Audio")

    def test_generate_title_media_voice(self):
        message = self._create_mock_message(media=MessageMediaType.VOICE)
        self.assertEqual(self.parser._generate_title(message), "🎤 Voice")

    def test_generate_title_media_video_note(self):
        message = self._create_mock_message(media=MessageMediaType.VIDEO_NOTE)
        self.assertEqual(self.parser._generate_title(message), "📱 Video circle")

    def test_generate_title_media_sticker(self):
        message = self._create_mock_message(media=MessageMediaType.STICKER)
        self.assertEqual(self.parser._generate_title(message), "🎯 Sticker")

    def test_generate_title_media_pdf_document(self):
        message = self._create_mock_message(media=MessageMediaType.DOCUMENT, document_mime_type='application/pdf')
        self.assertEqual(self.parser._generate_title(message), "📄 PDF Document")

    def test_generate_title_media_other_document(self):
        message = self._create_mock_message(media=MessageMediaType.DOCUMENT, document_mime_type='image/jpeg')
        self.assertEqual(self.parser._generate_title(message), "📎 Document")

    def test_generate_title_channel_created(self):
        message = self._create_mock_message(channel_chat_created=True)
        self.assertEqual(self.parser._generate_title(message), "✨ Channel created")

    def test_generate_title_text_only(self):
        message = self._create_mock_message(text="This is the first line.\nThis is the second line.")
        self.assertEqual(self.parser._generate_title(message), "This is the first line") #first line and remove dot

    def test_generate_title_text_with_html(self):
        message = self._create_mock_message(text="<b>Bold</b> text first line.\n<i>Italic</i> second line.")
        self.assertEqual(self.parser._generate_title(message), "Bold text first line") #first line and remove tags and dot

    def test_generate_title_text_with_url_and_text(self):
        message = self._create_mock_message(text="Check out this link: https://example.com")
        self.assertEqual(self.parser._generate_title(message), "Check out this link:") # URL is stripped

    def test_generate_title_text_only_url(self):
        message = self._create_mock_message(text="https://example.com")
        self.assertEqual(self.parser._generate_title(message), "🔗 Web link")

    def test_generate_title_text_only_youtube_url(self):
        message = self._create_mock_message(text="https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        self.assertEqual(self.parser._generate_title(message), "🎥 YouTube Link")

    def test_generate_title_text_only_youtu_be_url(self):
        message = self._create_mock_message(text="https://youtu.be/dQw4w9WgXcQ")
        self.assertEqual(self.parser._generate_title(message), "🎥 YouTube Link")

    def test_generate_title_caption_with_url_and_text(self):
        message = self._create_mock_message(media=MessageMediaType.PHOTO, caption="Look at this photo! https://example.com/image.jpg")
        self.assertEqual(self.parser._generate_title(message), "Look at this photo!")

    def test_generate_title_caption_with_uppercase_text(self):
        message = self._create_mock_message(text="ЖИЗНЬ НА ОБОЯХ")
        self.assertEqual(self.parser._generate_title(message), "Жизнь на обоях") #downcase 

    def test_generate_title_long_text_trimming(self):
        long_text = "This is a very long line of text that definitely exceeds the maximum length allowed for a title, so it should be trimmed intelligently at the last space before the limit."
        message = self._create_mock_message(text=long_text)
        expected_title = "This is a very long line of text that..." #cut at 30 symbols
        self.assertEqual(self.parser._generate_title(message), expected_title)

    def test_generate_title_long_text_no_space_trimming(self):
        long_text = "Thisisaverylonglineoftextthatdefinitelyexceedsthemaximumlengthallowedforatitlesoitshouldbetrimmedatthelimitbecausehasnospaces."
        message = self._create_mock_message(text=long_text)
        expected_title = "Thisisaverylonglineoftextthatdefinite..." #cut at 30 symbols
        self.assertEqual(self.parser._generate_title(message), expected_title)

    def test_generate_title_text_with_only_html_and_urls(self):
        message = self._create_mock_message(text="<a href='https://example.com'>Link name</a> https://another.link")
        self.assertEqual(self.parser._generate_title(message), "Link name")

    def test_generate_title_empty_text_after_cleaning(self):
        message = self._create_mock_message(text="<br>  https://link.com \n ")
        self.assertEqual(self.parser._generate_title(message), "🔗 Web link")


    def test_generate_title_webpage_preview_ignored_with_media(self):
        web_page_mock = MagicMock()
        message = self._create_mock_message(media=MessageMediaType.PHOTO, web_page=web_page_mock)
        self.assertEqual(self.parser._generate_title(message), "📷 Photo") # Media has higher priority

    def test_generate_title_webpage_preview_ignored_with_text(self):
        web_page_mock = MagicMock()
        message = self._create_mock_message(text="Some text", web_page=web_page_mock)
        self.assertEqual(self.parser._generate_title(message), "Some text") # Text has higher priority than fallback web page

    def test_generate_title_fallback_unknown(self):
        message = self._create_mock_message() # No text, no media, no webpage
        # Use variable to capture the actual value
        result = self.parser._generate_title(message)
        self.assertIn("Unknown Post", result)  # Just check if it contains "Unknown Post"

    def test_generate_title_poll_media_type(self):
        # Create a mock poll object with question
        poll_mock = MagicMock()
        poll_mock.question = "Как правильно?"
        message = self._create_mock_message(media=MessageMediaType.POLL, poll=poll_mock)
        
        # Test should check that poll.question is used for title
        self.assertEqual(self.parser._generate_title(message), "📊 Poll: Как правильно?")

    def test_generate_title_webpage_media_type(self):
        # Webpage media type should be ignored for title generation, text should be used
        message = self._create_mock_message(media=MessageMediaType.WEB_PAGE, text="Check this out")
        self.assertEqual(self.parser._generate_title(message), "Check this out")

    def test_generate_title_webpage_with_url_text(self):
        """Test case that reproduces real issue with VK video link."""
        # Создаем мок для web_page
        web_page_mock = MagicMock()
        web_page_mock.title = "Web page title"
        
        # Создаем сообщение с текстом URL и mock web_page
        message = self._create_mock_message(
            web_page=web_page_mock, 
            text="https://vkvideo.ru/video295754128_456239449"
        )
        
        self.assertEqual(self.parser._generate_title(message), "🔗 Web page title")
    
    def test_generate_title_webpage_without_url_text(self):
        web_page_mock = MagicMock()
        web_page_mock.title = "Web page title"
        message = self._create_mock_message(web_page=web_page_mock) # Text is whitespace only
        self.assertEqual(self.parser._generate_title(message), "🔗 Web page title")

if __name__ == '__main__':
    unittest.main() 