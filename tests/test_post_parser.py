# pylint: disable=protected-access, wrong-import-position

import unittest
from unittest.mock import MagicMock, PropertyMock
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

    def _create_mock_message(self, media=None, text=None, caption=None, document_mime_type=None, channel_chat_created=False, web_page=None, poll=None, service=None):
        message = MagicMock(spec=Message)
        message.media = media
        message.text = text
        message.caption = caption
        message.web_page = web_page
        message.channel_chat_created = channel_chat_created
        message.poll = poll
        message.service = service

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
        mock_service = MagicMock()
        mock_service.__str__ = MagicMock(return_value="pyrogram.enums.MessageService.CHANNEL_CHAT_CREATED")
        message = self._create_mock_message(service=mock_service, channel_chat_created=False) # channel_chat_created flag is now secondary
        self.assertEqual(self.parser._generate_title(message), "✨ Chat created")

    def test_generate_title_pinned_message(self):
        mock_service = MagicMock()
        mock_service.__str__ = MagicMock(return_value="pyrogram.enums.MessageService.PINNED_MESSAGE")
        message = self._create_mock_message(service=mock_service)
        self.assertEqual(self.parser._generate_title(message), "📌 Pinned message")

    def test_generate_title_new_chat_photo(self):
        mock_service = MagicMock()
        mock_service.__str__ = MagicMock(return_value="pyrogram.enums.MessageService.NEW_CHAT_PHOTO")
        message = self._create_mock_message(service=mock_service)
        self.assertEqual(self.parser._generate_title(message), "🖼 New chat photo")

    def test_generate_title_new_chat_title(self):
        mock_service = MagicMock()
        mock_service.__str__ = MagicMock(return_value="pyrogram.enums.MessageService.NEW_CHAT_TITLE")
        message = self._create_mock_message(service=mock_service)
        self.assertEqual(self.parser._generate_title(message), "✏️ New chat title")

    def test_generate_title_video_chat_started(self):
        mock_service = MagicMock()
        mock_service.__str__ = MagicMock(return_value="pyrogram.enums.MessageService.VIDEO_CHAT_STARTED")
        message = self._create_mock_message(service=mock_service)
        self.assertEqual(self.parser._generate_title(message), "▶️ Video chat started")

    def test_generate_title_video_chat_ended(self):
        mock_service = MagicMock()
        mock_service.__str__ = MagicMock(return_value="pyrogram.enums.MessageService.VIDEO_CHAT_ENDED")
        message = self._create_mock_message(service=mock_service)
        self.assertEqual(self.parser._generate_title(message), "⏹ Video chat ended")

    def test_generate_title_video_chat_scheduled(self):
        mock_service = MagicMock()
        mock_service.__str__ = MagicMock(return_value="pyrogram.enums.MessageService.VIDEO_CHAT_SCHEDULED")
        message = self._create_mock_message(service=mock_service)
        self.assertEqual(self.parser._generate_title(message), "⏰ Video chat scheduled")

    def test_generate_title_group_chat_created(self):
        mock_service = MagicMock()
        mock_service.__str__ = MagicMock(return_value="pyrogram.enums.MessageService.GROUP_CHAT_CREATED")
        message = self._create_mock_message(service=mock_service)
        self.assertEqual(self.parser._generate_title(message), "✨ Group chat created")

    def test_generate_title_delete_chat_photo(self):
        mock_service = MagicMock()
        mock_service.__str__ = MagicMock(return_value="pyrogram.enums.MessageService.DELETE_CHAT_PHOTO")
        message = self._create_mock_message(service=mock_service)
        self.assertEqual(self.parser._generate_title(message), "🗑️ Chat photo deleted")

    def test_generate_title_text_only(self):
        message = self._create_mock_message(text="This is the first line.\nThis is the second line.")
        self.assertEqual(self.parser._generate_title(message), "This is the first line") #first line and remove dot

    def test_generate_title_text_with_html(self):
        message = self._create_mock_message(text="<b>Bold</b> text first line.\n<i>Italic</i> second line.")
        self.assertEqual(self.parser._generate_title(message), "Bold text first line") #first line and remove tags and dot

    def test_generate_title_text_with_url_and_text(self):
        message = self._create_mock_message(text="Check out this link: https://example.com")
        self.assertEqual(self.parser._generate_title(message), "Check out this link") # URL is stripped

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


    def test_generate_title_long_text_trimming_with_spaces(self):
        #cut_at = 37
        #max_extra = 15
        #limit = cut_at + max_extra # 52

        # --- Test Cases Based on Correct Logic ---

        # 1. Length <= cut_at (37) -> No trim
        text = "This text is exactly thirty-seven chars" # len 37
        message = self._create_mock_message(text=text)
        self.assertEqual(self.parser._generate_title(message), text)

        # 2. Length = 38, no space in check range -> NO Trim (cut index == len)
        text = "This text is exactly thirty-seven charsX" # len 38
        message = self._create_mock_message(text=text)
        self.assertEqual(self.parser._generate_title(message), text) 

        # 3. Space found within range [cut_at, limit)
        # 3a. Space exactly at cut_at (index 37)
        text = "This text is exactly thirty-seven chars next" # len 42. Space at 37.
        message = self._create_mock_message(text=text)
        # Loop range(37, 42) -> i=37. first_line[37]==' '. Break. ext_cut=37. Slice [:37].
        self.assertEqual(self.parser._generate_title(message), "This text is exactly thirty-seven chars...")

        # 3b. 
        text = "This text is quite a bit longer now space_here_herehere" 
        message = self._create_mock_message(text=text)
        # Loop range(37, 49). Finds space at i=43. Breaks. ext_cut=43. Slice [:43].
        self.assertEqual(self.parser._generate_title(message), "This text is quite a bit longer now space_here_hereh...")

        # 3c. Space exactly at limit - 1 (index 51)
        text = "This is fifty-one characters long with the space1 here X" # len 54. Space at 51.
        message = self._create_mock_message(text=text)
        # Loop range(37, 52). Finds space at i=51. Breaks. ext_cut=51. Slice [:51].
        self.assertEqual(self.parser._generate_title(message), "This is fifty-one characters long with the space1...")

        # 3c. Space exactly at limit - 1 (index 51)
        text = "This is fifty-one chara the space1 here1 X" # len 54. Space at 51.
        message = self._create_mock_message(text=text)
        # Loop range(37, 52). Finds space at i=51. Breaks. ext_cut=51. Slice [:51].
        self.assertEqual(self.parser._generate_title(message), "This is fifty-one chara the space1 here1...")

        # 4. No space found within range [cut_at, limit)
        # 4a. Length > cut_at, Length < limit. No space in [cut_at, len). -> Cut at len-1
        text = "JGHJHKJHKJDHfushdkjfskjdfhnksjdvnskjdnkjsdfjksdhfsdlfijoirukjvnsdkjvskufh" # len 49. cut_at=37. limit=52.
        message = self._create_mock_message(text=text)
        # Loop range(37, 49). No space. Finishes. ext_cut=48. Slice [:48].
        self.assertEqual(self.parser._generate_title(message), "JGHJHKJHKJDHfushdkjfskjdfhnksjdvnskjdnkjsdfjksdhfsdl...")

        text = "JGHJHKJHKJDHfushdkjfskjdfhnksjdvnskjdnkjsdfjksdhfs" # len 49. cut_at=37. limit=52.
        message = self._create_mock_message(text=text)
        self.assertEqual(self.parser._generate_title(message), "JGHJHKJHKJDHfushdkjfskjdfhnksjdvnskjdnkjsdfjksdhfs")

        # 4b. Length >= limit. No space in [cut_at, limit). -> Cut at limit-1 = 51
        text = "ThisIsAnEvenLongerWordWithoutAnySpacesAndDefinitelyMoreThan52Chars" # len 66. cut_at=37. limit=52.
        message = self._create_mock_message(text=text)
        # Loop range(37, 52). No space. Finishes. ext_cut=51. Slice [:51].
        self.assertEqual(self.parser._generate_title(message), "ThisIsAnEvenLongerWordWithoutAnySpacesAndDefinitelyM...")

        # 5. Trailing space/punctuation removal check
        # 5a. Space found, cut segment ends with space/punct
        text = "This text is quite a bit longer now, space .,;: here" # len 54. Space at 43.
        message = self._create_mock_message(text=text)
        # Loop range(37, 54). Finds space at i=43. Breaks. ext_cut=43. Slice [:43] is "This text is quite a bit longer now, space ".
        # re.sub removes trailing " .,;: ". Result "This text is quite a bit longer now, space".
        self.assertEqual(self.parser._generate_title(message), "This text is quite a bit longer now, space...")

    def test_generate_title_break_word_after_limit(self):
        # Test with a specific text example from the user's query
        text = "На прошлой неделе предложил своим подписчикам рассказать, как бы они хотели улучшить функциональность государственного сервиса"
        message = self._create_mock_message(text=text)
        expected_title = "На прошлой неделе предложил своим подписчикам..."
        self.assertEqual(self.parser._generate_title(message), expected_title)

    def test_generate_title_long_text_no_space_trimming(self):
        long_text = "Thisisaverylonglineoftextthatdefinitelyexceedsthemaximumlengthallowedforatitlesoitshouldbetrimmedatthelimitbecausehasnospaces."
        message = self._create_mock_message(text=long_text)
        # Corrected expected title based on new logic: cut at index 52 (min(52, len)) -> slice[:52]
        expected_title = "Thisisaverylonglineoftextthatdefinitelyexceedsthemax..."
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
        # Webpage media type should be ignored for title generation if there's enough text
        message = self._create_mock_message(media=MessageMediaType.WEB_PAGE, text="Check this out it is long enough")
        self.assertEqual(self.parser._generate_title(message), "Check this out it is long enough")

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

    def test_generate_title_punctuation_removal(self):
        """Test removing punctuation marks from the end of titles."""
        test_cases = {
            # Одиночные символы
            "Привет.": "Привет",
            "Привет,": "Привет",
            "Привет;": "Привет",
            "Привет:": "Привет",
            "Привет!": "Привет!",
            
            # Комбинация символов
            "Привет...": "Привет",
            "Привет.,;:": "Привет",
            "Привет.!": "Привет.!",
            "Строка.....": "Строка",
            
            # Символы не в конце не должны удаляться
            "При.вет": "При.вет",
            "При,вет": "При,вет",
            "При;вет": "При;вет",
            "При:вет": "При:вет",
            "При!вет": "При!вет",
            "При\"вет": "При\"вет",
            "При'вет": "При'вет",
            "Привет": "Привет",
            
            # Строки с пробелами после пунктуации
            "Привет. ": "Привет",
            "Привет, ": "Привет",
            
            # Языки
            "Привет на русском.": "Привет на русском",
            "Hello.": "Hello",
            "Hello,": "Hello",
            "Привіт українською.": "Привіт українською",
            "Hola en español.": "Hola en español",
            
            # Цифры
            "Число 123.": "Число 123",
            
            # Многострочный текст
            "Привет.\nКак дела?": "Привет",
            
            # Сложные случаи с кавычками
            "Текст с \"кавычками внутри\".": "Текст с \"кавычками внутри\"",
            "Текст с 'одинарными' кавычками.": "Текст с 'одинарными' кавычками",
            "Текст с \"вложенными 'кавычками'\".": "Текст с \"вложенными 'кавычками'\"",
            "Цитата: \"Это цитата.\".": "Цитата: \"Это цитата.\"",
            
            # Специальные случаи
            "Предложение с восклицанием!": "Предложение с восклицанием!",
            "Предложение с вопросом?": "Предложение с вопросом?", 
            "Эллипсис...": "Эллипсис",
            
            "Конец текста.,;:": "Конец текста",
            "Много точек....": "Много точек",
            "Разные знаки.,;": "Разные знаки",
            
            # Реальные примеры
            "Анонс конференции:": "Анонс конференции",
            "Новый релиз v1.0!": "Новый релиз v1.0!",
            "Важная информация!!!": "Важная информация!!!",
            "Автор сказал: \"Это важно\".": "Автор сказал: \"Это важно\"",
            "Код программы: function() { return true; }": "Код программы: function() { return true...",
            
            # Проверяем краевые случаи
            "Статья...": "Статья",
            "Вопросы и ответы.": "Вопросы и ответы",
            "Конец,": "Конец",
        }
        
        for input_text, expected_output in test_cases.items():
            message = self._create_mock_message(text=input_text)
            title = self.parser._generate_title(message)
            self.assertEqual(title, expected_output, f"Ошибка при обработке '{input_text}': получено '{title}', ожидалось '{expected_output}'")

    # --- New tests for text length logic ---

    def test_generate_title_media_with_short_caption(self):
        """Media title should be used if caption is short (< 10 chars)."""
        message = self._create_mock_message(media=MessageMediaType.PHOTO, caption="Hi <3")
        self.assertEqual(self.parser._generate_title(message), "📷 Photo")

    def test_generate_title_media_with_long_text(self):
        """Text title should be used if text is long (>= 10 chars), ignoring media."""
        message = self._create_mock_message(media=MessageMediaType.VIDEO, text="This is a sufficiently long text.")
        self.assertEqual(self.parser._generate_title(message), "This is a sufficiently long text")

    def test_generate_title_media_with_long_caption(self):
        """Text title from caption should be used if caption is long (>= 10 chars), ignoring media."""
        message = self._create_mock_message(media=MessageMediaType.PHOTO, caption="This is a sufficiently long caption.")
        self.assertEqual(self.parser._generate_title(message), "This is a sufficiently long caption")

    def test_generate_title_no_media_with_short_text(self):
        """Text title should be used if no media and text is short."""
        message = self._create_mock_message(text="Short one")
        self.assertEqual(self.parser._generate_title(message), "Short one")

    def test_generate_title_no_media_with_long_text(self):
        """Text title should be used if no media and text is long."""
        message = self._create_mock_message(text="This is a long text without any media.")
        self.assertEqual(self.parser._generate_title(message), "This is a long text without any media")

    def test_generate_title_media_with_no_text(self):
        """Media title should be used if media exists and text is None."""
        message = self._create_mock_message(media=MessageMediaType.STICKER, text=None)
        self.assertEqual(self.parser._generate_title(message), "🎯 Sticker")

    def test_generate_title_media_with_empty_text(self):
        """Media title should be used if media exists and text is empty string."""
        message = self._create_mock_message(media=MessageMediaType.AUDIO, text="")
        self.assertEqual(self.parser._generate_title(message), "🎵 Audio")

    def test_generate_title_media_with_whitespace_text(self):
        """Media title should be used if media exists and text is only whitespace."""
        message = self._create_mock_message(media=MessageMediaType.VOICE, text="   \\n  ")
        self.assertEqual(self.parser._generate_title(message), "🎤 Voice")

    def test_generate_title_service_message_overrides_long_text(self):
        """Service message title should override even long text."""
        mock_service = MagicMock()
        mock_service.__str__ = MagicMock(return_value="pyrogram.enums.MessageService.PINNED_MESSAGE")
        message = self._create_mock_message(service=mock_service, text="This is a long text but should be ignored.")
        self.assertEqual(self.parser._generate_title(message), "📌 Pinned message")

    def test_generate_title_webpage_with_short_text(self):
        """Text title should be used if web_page exists and text is short (but not just URL)."""
        web_page_mock = MagicMock()
        web_page_mock.title = "Web Page Title To Ignore"
        message = self._create_mock_message(web_page=web_page_mock, text="Short txt")
        self.assertEqual(self.parser._generate_title(message), "Short txt")

# --- End new tests ---

if __name__ == '__main__':
    unittest.main() 
