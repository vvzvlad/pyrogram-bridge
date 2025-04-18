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

        # Ensure forward_origin is always present
        message.forward_origin = None

        return message

    def gen_title(self, text):
        message = self._create_mock_message(text=text)
        return self.parser._generate_title(message)
    
    def gen_media_title(self, media):
        message = self._create_mock_message(media=media)
        return self.parser._generate_title(message)

    def test_generate_title_media_photo(self):
        self.assertEqual(self.gen_media_title(MessageMediaType.PHOTO), "📷 Photo")
    def test_generate_title_media_video(self):
        self.assertEqual(self.gen_media_title(MessageMediaType.VIDEO), "🎥 Video")
    def test_generate_title_media_animation(self):
        self.assertEqual(self.gen_media_title(MessageMediaType.ANIMATION), "🎞 GIF")
    def test_generate_title_media_audio(self):
        self.assertEqual(self.gen_media_title(MessageMediaType.AUDIO), "🎵 Audio")
    def test_generate_title_media_voice(self):
        self.assertEqual(self.gen_media_title(MessageMediaType.VOICE), "🎤 Voice")
    def test_generate_title_media_video_note(self):
        self.assertEqual(self.gen_media_title(MessageMediaType.VIDEO_NOTE), "📱 Video circle")
    def test_generate_title_media_sticker(self):
        self.assertEqual(self.gen_media_title(MessageMediaType.STICKER), "🎯 Sticker")

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


    def test_generate_title_long_text_trimming_exact_37(self):
        text = "This text is exactly thirty-seven chars"
        self.assertEqual(self.gen_title(text), text)

    def test_generate_title_long_text_trimming_38_no_space(self):
        text = "This text is exactly thirty-seven charsX"
        self.assertEqual(self.gen_title(text), text)

    def test_generate_title_long_text_trimming_space_at_37(self):
        text = "This text is exactly thirty-seven chars next"
        self.assertEqual(self.gen_title(text), "This text is exactly thirty-seven chars...")

    def test_generate_title_long_text_trimming_space_at_43(self):
        text = "This text is quite a bit longer now space_here_herehere"
        self.assertEqual(self.gen_title(text), "This text is quite a bit longer now space_here_hereh...")

    def test_generate_title_long_text_trimming_space_at_51(self):
        text = "This is fifty-one characters long with the space1 here X"
        self.assertEqual(self.gen_title(text), "This is fifty-one characters long with the space1...")

    def test_generate_title_long_text_trimming_space_at_51_alt(self):
        text = "This is fifty-one chara the space1 here1 X"
        self.assertEqual(self.gen_title(text), "This is fifty-one chara the space1 here1...")

    def test_generate_title_long_text_trimming_no_space_cut_at_48(self):
        text = "JGHJHKJHKJDHfushdkjfskjdfhnksjdvnskjdnkjsdfjksdhfsdlfijoirukjvnsdkjvskufh"
        self.assertEqual(self.gen_title(text), "JGHJHKJHKJDHfushdkjfskjdfhnksjdvnskjdnkjsdfjksdhfsdl...")

    def test_generate_title_long_text_trimming_no_space_cut_at_49(self):
        text = "JGHJHKJHKJDHfushdkjfskjdfhnksjdvnskjdnkjsdfjksdhfs"
        self.assertEqual(self.gen_title(text), "JGHJHKJHKJDHfushdkjfskjdfhnksjdvnskjdnkjsdfjksdhfs")

    def test_generate_title_long_text_trimming_no_space_cut_at_51(self):
        text = "ThisIsAnEvenLongerWordWithoutAnySpacesAndDefinitelyMoreThan52Chars"
        self.assertEqual(self.gen_title(text), "ThisIsAnEvenLongerWordWithoutAnySpacesAndDefinitelyM...")

    def test_generate_title_long_text_trimming_trailing_punct(self):
        text = "This text is quite a bit longer now, space .,;: here"
        self.assertEqual(self.gen_title(text), "This text is quite a bit longer now, space...")

    def test_generate_title_break_word_after_limit(self):
        message = self._create_mock_message(text="На прошлой неделе предложил своим подписчикам рассказать, как бы они хотели ул")
        self.assertEqual(self.parser._generate_title(message), "На прошлой неделе предложил своим подписчикам...")

    def test_generate_title_long_text_no_space_trimming(self):
        long_text = "Thisisaverylonglineoftextthatdefinitelyexceedsthemaximumlengthallowedforatitlesoitshouldbetrimmedatthelimitbecausehasnospaces."
        message = self._create_mock_message(text=long_text)
        self.assertEqual(self.parser._generate_title(message), "Thisisaverylonglineoftextthatdefinitelyexceedsthemax...")

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

    def test_generate_title_punctuation_removal_dot(self):
        self.assertEqual(self.gen_title("Привет."), "Привет")

    def test_generate_title_punctuation_removal_comma(self):
        self.assertEqual(self.gen_title("Привет,"), "Привет")

    def test_generate_title_punctuation_removal_semicolon(self):
        self.assertEqual(self.gen_title("Привет;"), "Привет")

    def test_generate_title_punctuation_removal_colon(self):
        self.assertEqual(self.gen_title("Привет:"), "Привет")

    def test_generate_title_punctuation_removal_exclamation(self):
        self.assertEqual(self.gen_title("Привет!"), "Привет!")

    def test_generate_title_punctuation_removal_ellipsis(self):
        self.assertEqual(self.gen_title("Привет..."), "Привет")

    def test_generate_title_punctuation_removal_combo(self):
        self.assertEqual(self.gen_title("Привет.,;:"), "Привет")

    def test_generate_title_punctuation_removal_dot_exclamation(self):
        self.assertEqual(self.gen_title("Привет.!"), "Привет.!")

    def test_generate_title_punctuation_removal_many_dots(self):
        self.assertEqual(self.gen_title("Строка....."), "Строка")

    def test_generate_title_punctuation_removal_inner_punctuation_dot(self):
        self.assertEqual(self.gen_title("При.вет"), "При.вет")
    def test_generate_title_punctuation_removal_inner_punctuation_comma(self):
        self.assertEqual(self.gen_title("При,вет"), "При,вет")
    def test_generate_title_punctuation_removal_inner_punctuation_semicolon(self):
        self.assertEqual(self.gen_title("При;вет"), "При;вет")
    def test_generate_title_punctuation_removal_inner_punctuation_colon(self):
        self.assertEqual(self.gen_title("При:вет"), "При:вет")
    def test_generate_title_punctuation_removal_inner_punctuation_exclamation(self):
        self.assertEqual(self.gen_title("При!вет"), "При!вет")
    def test_generate_title_punctuation_removal_inner_punctuation_doublequote(self):
        self.assertEqual(self.gen_title("При\"вет"), "При\"вет")
    def test_generate_title_punctuation_removal_inner_punctuation_singlequote(self):
        self.assertEqual(self.gen_title("При'вет"), "При'вет")
    def test_generate_title_punctuation_removal_inner_punctuation_plain(self):
        self.assertEqual(self.gen_title("Привет"), "Привет")

    def test_generate_title_punctuation_removal_languages_ru(self):
        self.assertEqual(self.gen_title("Привет на русском."), "Привет на русском")
    def test_generate_title_punctuation_removal_languages_en(self):
        self.assertEqual(self.gen_title("Hello."), "Hello")
    def test_generate_title_punctuation_removal_languages_en_comma(self):
        self.assertEqual(self.gen_title("Hello,"), "Hello")
    def test_generate_title_punctuation_removal_languages_ua(self):
        self.assertEqual(self.gen_title("Привіт українською."), "Привіт українською")
    def test_generate_title_punctuation_removal_languages_es(self):
        self.assertEqual(self.gen_title("Hola en español."), "Hola en español")

    def test_generate_title_punctuation_removal_quotes_double(self):
        self.assertEqual(self.gen_title('Текст с "кавычками внутри".'), 'Текст с "кавычками внутри"')
    def test_generate_title_punctuation_removal_quotes_single(self):
        self.assertEqual(self.gen_title("Текст с 'одинарными' кавычками."), "Текст с 'одинарными' кавычками")
    def test_generate_title_punctuation_removal_quotes_nested(self):
        self.assertEqual(self.gen_title('Текст с "вложенными \'кавычками\'".'), 'Текст с "вложенными \'кавычками\'"')
    def test_generate_title_punctuation_removal_quotes_citation(self):
        self.assertEqual(self.gen_title('Цитата: "Это цитата.".'), 'Цитата: "Это цитата."')

    def test_generate_title_punctuation_removal_special_exclamation(self):
        self.assertEqual(self.gen_title("Предложение с восклицанием!"), "Предложение с восклицанием!")
    def test_generate_title_punctuation_removal_special_question(self):
        self.assertEqual(self.gen_title("Предложение с вопросом?"), "Предложение с вопросом?")
    def test_generate_title_punctuation_removal_special_ellipsis(self):
        self.assertEqual(self.gen_title("Эллипсис..."), "Эллипсис")
    def test_generate_title_punctuation_removal_special_combo(self):
        self.assertEqual(self.gen_title("Конец текста.,;:"), "Конец текста")
    def test_generate_title_punctuation_removal_special_manydots(self):
        self.assertEqual(self.gen_title("Много точек...."), "Много точек")
    def test_generate_title_punctuation_removal_special_mix(self):
        self.assertEqual(self.gen_title("Разные знаки.,;"), "Разные знаки")

    def test_generate_title_punctuation_removal_real_examples_announce(self):
        self.assertEqual(self.gen_title("Анонс конференции:"), "Анонс конференции")
    def test_generate_title_punctuation_removal_real_examples_release(self):
        self.assertEqual(self.gen_title("Новый релиз v1.0!"), "Новый релиз v1.0!")
    def test_generate_title_punctuation_removal_real_examples_info(self):
        self.assertEqual(self.gen_title("Важная информация!!!"), "Важная информация!!!")
    def test_generate_title_punctuation_removal_real_examples_author(self):
        self.assertEqual(self.gen_title('Автор сказал: "Это важно".'), 'Автор сказал: "Это важно"')
    def test_generate_title_punctuation_removal_real_examples_code(self):
        self.assertEqual(self.gen_title("Код программы: function() { return true; }"), "Код программы: function() { return true...")

    def test_generate_title_punctuation_removal_edge_cases_article(self):
        self.assertEqual(self.gen_title("Статья..."), "Статья")
    def test_generate_title_punctuation_removal_edge_cases_qa(self):
        self.assertEqual(self.gen_title("Вопросы и ответы."), "Вопросы и ответы")
    def test_generate_title_punctuation_removal_edge_cases_end(self):
        self.assertEqual(self.gen_title("Конец,"), "Конец")

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

    def test_generate_title_truncate_at_first_period(self):
        """Title should be truncated at the first period if present."""
        text = "⚡️ OpenAI сегодня представила o3/o4-mini. кажется, они сделали очень сильную ставку на \"агентскость\"."
        message = self._create_mock_message(text=text)
        expected_title = "⚡️ OpenAI сегодня представила o3/o4-mini"
        self.assertEqual(self.parser._generate_title(message), expected_title)


if __name__ == '__main__':
    unittest.main() 
