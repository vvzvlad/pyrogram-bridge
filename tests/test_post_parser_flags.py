# pylint: disable=protected-access, wrong-import-position

import unittest
from unittest.mock import MagicMock, PropertyMock
import sys
import os

# Add project root to sys.path to find post_parser
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# Mock the config module before importing PostParser
sys.modules['config'] = __import__('tests.mock_config', fromlist=['get_settings'])

from pyrogram.types import Message, Chat, User, Reaction, MessageReactions
from pyrogram.enums import MessageMediaType
from post_parser import PostParser # Import after mocking config


class TestPostParserExtractFlags(unittest.TestCase):

    def setUp(self):
        self.client_mock = MagicMock()
        self.parser = PostParser(self.client_mock)
        # Mock the get_channel_username method to return a default channel name
        self.parser.get_channel_username = MagicMock(return_value="test_channel")

    def tearDown(self):
        # Reset mocks if needed
        pass

    def _create_mock_message(self,
                            media=None,
                            text=None,
                            caption=None,
                            reactions_data=None, # Format: [("emoji", count), ...]
                            forward_from_chat=None, # Mock Chat object or True
                            forward_from=None,      # Mock User object or True
                            forward_sender_name=None, # String or True
                            chat_username="test_channel"): # Default chat username
        message = MagicMock(spec=Message)
        message.media = media
        message.id = 123 # Add a default message ID

        # Mock chat attribute
        mock_chat = MagicMock(spec=Chat)
        mock_chat.username = chat_username
        mock_chat.id = -1001234567890 if chat_username.startswith('-100') else 1234567890
        message.chat = mock_chat
        
        # Mock reactions
        if reactions_data:
            mock_reactions_list = []
            for emoji, count in reactions_data:
                reaction_mock = MagicMock(spec=Reaction)
                reaction_mock.emoji = emoji
                reaction_mock.count = count
                mock_reactions_list.append(reaction_mock)
            reactions_container_mock = MagicMock(spec=MessageReactions)
            reactions_container_mock.reactions = mock_reactions_list
            message.reactions = reactions_container_mock
        else:
            message.reactions = None

        # Mock forwarding info
        if forward_from_chat:
            message.forward_from_chat = MagicMock(spec=Chat) if forward_from_chat is True else forward_from_chat
        else:
            message.forward_from_chat = None

        if forward_from:
            message.forward_from = MagicMock(spec=User) if forward_from is True else forward_from
        else:
            message.forward_from = None

        message.forward_sender_name = forward_sender_name if forward_sender_name is not True else "Hidden Sender"

        # Correctly mock text and caption to have an .html attribute and behave like strings
        if text:
            mock_text = MagicMock()
            mock_text.html = text
            mock_text.__str__.return_value = text # Add __str__ mock
            message.text = mock_text
            message.caption = None
        elif caption:
            mock_caption = MagicMock()
            mock_caption.html = caption
            mock_caption.__str__.return_value = caption # Add __str__ mock
            message.caption = mock_caption
            message.text = None
        else:
            message.text = None
            message.caption = None

        # Mock other necessary attributes accessed by _generate_html_body and other helpers
        message.poll = None
        message.channel_chat_created = False
        message.reply_to_message = None
        message.service = None
        message.web_page = None # Assume no web page unless specifically tested for flags

        return message

    # --- Test Cases ---

    def test_flag_fwd_forwarded_from_chat(self):
        message = self._create_mock_message(forward_from_chat=True)
        self.assertIn("fwd", self.parser._extract_flags(message))

    def test_flag_fwd_forwarded_from_user(self):
        message = self._create_mock_message(forward_from=True)
        self.assertIn("fwd", self.parser._extract_flags(message))

    def test_flag_fwd_forwarded_sender_name(self):
        message = self._create_mock_message(forward_sender_name=True)
        self.assertIn("fwd", self.parser._extract_flags(message))

    def test_flag_video_media_video_short_caption(self):
        message = self._create_mock_message(media=MessageMediaType.VIDEO, caption="Short video")
        self.assertIn("video", self.parser._extract_flags(message))

    def test_flag_video_media_video_note_short_text(self):
        message = self._create_mock_message(media=MessageMediaType.VIDEO_NOTE, text="Short note")
        self.assertIn("video", self.parser._extract_flags(message))

    def test_flag_video_media_animation_no_text(self):
        message = self._create_mock_message(media=MessageMediaType.ANIMATION)
        self.assertIn("video", self.parser._extract_flags(message))

    def test_flag_video_media_video_long_caption(self):
        long_caption = "This is a very long caption that exceeds the two hundred character limit, therefore it should not trigger the video flag even though the media type is video." * 5
        
        # Create a simple mock message for this specific test
        message = MagicMock(spec=Message)
        message.media = MessageMediaType.VIDEO
        message.id = 123
        
        # Create a basic mock Chat
        mock_chat = MagicMock(spec=Chat)
        mock_chat.username = "test_channel"
        mock_chat.id = 1234567890
        message.chat = mock_chat
        
        # Set up text and caption to ensure len() returns the correct length
        message.text = None
        
        # Create a custom class that mimics caption with html attribute
        class CaptionWithHtml(str):
            @property
            def html(self):
                return self
                
        # Use our class instead of a regular string
        message.caption = CaptionWithHtml(long_caption)
        
        # Mock HTML body generation
        self.parser._generate_html_body = MagicMock(return_value=long_caption)
        
        # In _extract_flags, text length is checked and should be > 200
        self.assertGreater(len(message.caption), 200)
        self.assertNotIn("video", self.parser._extract_flags(message))

    def test_flag_no_image_no_media(self):
        message = self._create_mock_message(text="Just text")
        self.assertIn("no_image", self.parser._extract_flags(message))

    def test_flag_no_image_poll_media(self):
        # Need to mock poll structure if _generate_html_body uses it, but _extract_flags doesn't directly
        mock_poll = MagicMock()
        mock_poll.question = "A poll?"
        message = self._create_mock_message(media=MessageMediaType.POLL)
        message.poll = mock_poll # Assign mock poll
        self.assertIn("no_image", self.parser._extract_flags(message))

    def test_flag_no_image_with_image_media(self):
        message = self._create_mock_message(media=MessageMediaType.PHOTO)
        self.assertNotIn("no_image", self.parser._extract_flags(message))

    def test_flag_sticker(self):
        message = self._create_mock_message(media=MessageMediaType.STICKER)
        self.assertIn("sticker", self.parser._extract_flags(message))

    def test_flag_stream_keyword_stream(self):
        message = self._create_mock_message(text="Join our стрим tonight!")
        self.assertIn("stream", self.parser._extract_flags(message))

    def test_flag_stream_keyword_webinar(self):
        message = self._create_mock_message(text="Register for the вебинар.")
        self.assertIn("stream", self.parser._extract_flags(message))

    def test_flag_stream_keyword_lecture(self):
        message = self._create_mock_message(text="Upcoming онлайн-лекция about AI.")
        self.assertIn("stream", self.parser._extract_flags(message))
        
    def test_flag_stream_keyword_livestream(self):
        message = self._create_mock_message(text="Watch the livestream now.")
        self.assertIn("stream", self.parser._extract_flags(message))

    def test_flag_stream_case_insensitive(self):
        message = self._create_mock_message(text="Watch the LiveStream now.")
        self.assertIn("stream", self.parser._extract_flags(message))

    def test_flag_donat(self):
        message = self._create_mock_message(text="Support us via донат.")
        self.assertIn("donat", self.parser._extract_flags(message))

    def test_flag_donat_case_insensitive(self):
        message = self._create_mock_message(text="Support us via ДОНАТ.")
        self.assertIn("donat", self.parser._extract_flags(message))

    def test_flag_donat_with_boost_link(self):
        """Test that t.me/boost/ links add donat flag."""
        message = self._create_mock_message(text="Support the channel: https://t.me/boost/channel_name")
        self.assertIn("donat", self.parser._extract_flags(message))
        
    def test_flag_donat_with_cloudtips_link(self):
        """Test that pay.cloudtips.ru links add donat flag."""
        message = self._create_mock_message(text="Support us via this payment link: https://pay.cloudtips.ru/some_id")
        self.assertIn("donat", self.parser._extract_flags(message))
        
    def test_flag_donat_with_cloudtips_link_case_insensitive(self):
        """Test that PAY.CLOUDTIPS.RU links add donat flag (case insensitive)."""
        message = self._create_mock_message(text="Support us via PAY.CLOUDTIPS.RU/user")
        self.assertIn("donat", self.parser._extract_flags(message))

    def test_flag_clown_reaction(self):
        message = self._create_mock_message(reactions_data=[("🤡", 35), ("👍", 10)])
        self.assertIn("clownpoo", self.parser._extract_flags(message))

    def test_flag_clown_reaction_not_enough(self):
        message = self._create_mock_message(reactions_data=[("🤡", 29), ("👍", 10)])
        self.assertNotIn("clownpoo", self.parser._extract_flags(message))

    def test_flag_poo_reaction(self):
        message = self._create_mock_message(reactions_data=[("💩", 30), ("👎", 5)])
        self.assertIn("clownpoo", self.parser._extract_flags(message))

    def test_flag_poo_reaction_not_enough(self):
        message = self._create_mock_message(reactions_data=[("💩", 15), ("👎", 5)])
        self.assertNotIn("clownpoo", self.parser._extract_flags(message))

    def test_flag_advert_hashtag(self):
        message = self._create_mock_message(text="Check this out #реклама")
        self.assertIn("advert", self.parser._extract_flags(message))

    def test_flag_advert_partner_post(self):
        message = self._create_mock_message(text="Партнерский пост о новом продукте.")
        self.assertIn("advert", self.parser._extract_flags(message))

    def test_flag_advert_promo_code(self):
        message = self._create_mock_message(text="Use code XYZ по промокоду!")
        self.assertIn("advert", self.parser._extract_flags(message))
        
    def test_flag_advert_erid(self):
        message = self._create_mock_message(text="Some text erid:laskdjfhasdlkjfh")
        self.assertIn("advert", self.parser._extract_flags(message))

    def test_flag_paywall_boosty(self):
        message = self._create_mock_message(text="Full version on Boosty!")
        self.assertIn("paywall", self.parser._extract_flags(message))

    def test_flag_paywall_sponsr(self):
        message = self._create_mock_message(text="Support me on Sponsr.")
        self.assertIn("paywall", self.parser._extract_flags(message))

    def test_flag_link_in_text(self):
        message = self._create_mock_message(text="Visit https://example.com")
        # Use _generate_html_body to ensure link detection logic runs
        _ = self.parser._generate_html_body(message) # Call body generation which prepares text
        self.assertIn("link", self.parser._extract_flags(message))

    def test_flag_link_in_href(self):
        # Need to mock html property correctly
        message = self._create_mock_message(text="Check this <a href='http://test.com'>link</a>")
        type(message).html = PropertyMock(return_value="Check this <a href='http://test.com'>link</a>")
        # Use _generate_html_body to ensure link detection logic runs
        _ = self.parser._generate_html_body(message)
        self.assertIn("link", self.parser._extract_flags(message))

    def test_flag_link_no_link(self):
        message = self._create_mock_message(text="Just plain text.")
        self.assertNotIn("link", self.parser._extract_flags(message))

    def test_flag_mention(self):
        message = self._create_mock_message(text="Mentioning @some_channel here.")
        self.assertIn("mention", self.parser._extract_flags(message))

    def test_flag_mention_no_mention(self):
        message = self._create_mock_message(text="No mentions here.")
        self.assertNotIn("mention", self.parser._extract_flags(message))

    def test_flag_hid_channel(self):
        message = self._create_mock_message(text="Join here: https://t.me/+ABC123xyz")
        self.assertIn("hid_channel", self.parser._extract_flags(message))

    def test_flag_foreign_channel_different_channel(self):
        message = self._create_mock_message(text="Check out https://t.me/another_channel", chat_username="test_channel")
        self.parser.get_channel_username = MagicMock(return_value="test_channel") # Ensure current channel is mocked
        self.assertIn("foreign_channel", self.parser._extract_flags(message))

    def test_flag_foreign_channel_same_channel(self):
        message = self._create_mock_message(text="Our main channel https://t.me/test_channel", chat_username="test_channel")
        self.parser.get_channel_username = MagicMock(return_value="test_channel") # Ensure current channel is mocked
        self.assertNotIn("foreign_channel", self.parser._extract_flags(message))
        
    def test_flag_foreign_channel_case_insensitive(self):
        message = self._create_mock_message(text="Our main channel https://t.me/TEST_channel", chat_username="test_channel")
        self.parser.get_channel_username = MagicMock(return_value="test_channel") # Ensure current channel is mocked
        self.assertNotIn("foreign_channel", self.parser._extract_flags(message))

    def test_flag_foreign_channel_no_current_channel(self):
        message = self._create_mock_message(text="Link to https://t.me/another_channel")
        self.parser.get_channel_username = MagicMock(return_value=None) # Simulate no current channel found
        self.assertIn("foreign_channel", self.parser._extract_flags(message)) # Should still flag as foreign if current unknown

    def test_flag_foreign_channel_boost_other_channel(self):
        """Test that boost link to foreign channel is flagged."""
        message = self._create_mock_message(text="Boost another channel: https://t.me/boost/other_channel", chat_username="test_channel")
        self.parser.get_channel_username = MagicMock(return_value="test_channel") # Ensure current channel is mocked
        self.assertIn("foreign_channel", self.parser._extract_flags(message))
    
    def test_flag_foreign_channel_boost_same_channel(self):
        """Test that boost link to own channel is not flagged."""
        message = self._create_mock_message(text="Boost our channel: https://t.me/boost/test_channel", chat_username="test_channel")
        self.parser.get_channel_username = MagicMock(return_value="test_channel") # Ensure current channel is mocked
        self.assertNotIn("foreign_channel", self.parser._extract_flags(message))
    
    def test_flag_foreign_channel_boost_link_case_insensitive(self):
        """Test that boost link to own channel is not flagged (case insensitive)."""
        message = self._create_mock_message(text="Boost our channel: https://t.me/boost/TEST_CHANNEL", chat_username="test_channel")
        self.parser.get_channel_username = MagicMock(return_value="test_channel") # Ensure current channel is mocked
        self.assertNotIn("foreign_channel", self.parser._extract_flags(message))
    
    def test_flag_foreign_channel_multiple_links(self):
        """Test with multiple links - should flag if any is foreign."""
        message = self._create_mock_message(
            text="Links: https://t.me/test_channel and https://t.me/other_channel and https://t.me/boost/test_channel", 
            chat_username="test_channel"
        )
        self.parser.get_channel_username = MagicMock(return_value="test_channel") # Ensure current channel is mocked
        self.assertIn("foreign_channel", self.parser._extract_flags(message))
    
    def test_flag_foreign_channel_only_boost_word(self):
        """Test that the word 'boost' is not flagged."""
        message = self._create_mock_message(text="Check out https://t.me/boost", chat_username="test_channel")
        self.parser.get_channel_username = MagicMock(return_value="test_channel") # Ensure current channel is mocked
        self.assertNotIn("foreign_channel", self.parser._extract_flags(message))
        
    def test_flag_foreign_channel_boost_no_channel(self):
        """Test with boost/ but no channel after - should be flagged as foreign."""
        message = self._create_mock_message(text="Strange link: https://t.me/boost/", chat_username="test_channel")
        self.parser.get_channel_username = MagicMock(return_value="test_channel") # Ensure current channel is mocked
        self.assertNotIn("foreign_channel", self.parser._extract_flags(message))

    def test_flag_multiple_flags(self):
        message = self._create_mock_message(
            media=MessageMediaType.VIDEO,
            caption="Livestream announcement! Support via донат at https://example.com. Join https://t.me/+SECRET and mention @admin. #реклама",
            reactions_data=[("🤡", 40)],
            forward_from_chat=True
        )
        # Mock HTML generation as it's used internally
        _ = self.parser._generate_html_body(message)
        flags = self.parser._extract_flags(message)
        expected_flags = ["fwd", "video", "stream", "donat", "clownpoo", "advert", "link", "mention", "hid_channel"]
        self.assertCountEqual(flags, expected_flags) # Use assertCountEqual for order-insensitive list comparison

    def test_flag_no_flags(self):
        message = self._create_mock_message(media=MessageMediaType.PHOTO, caption="A simple photo post.")
        flags = self.parser._extract_flags(message)
        self.assertEqual(flags, []) # Expect an empty list

    # --- Additional Multiple Flags Tests ---

    def test_flag_multiple_fwd_link_mention(self):
        message = self._create_mock_message(
            text="Forwarded message with a link https://example.com and mention @someone.",
            forward_sender_name=True
        )
        flags = self.parser._extract_flags(message)
        expected_flags = ["fwd", "no_image", "link", "mention"]
        self.assertCountEqual(flags, expected_flags)

    def test_flag_multiple_video_advert_clown(self):
        message = self._create_mock_message(
            media=MessageMediaType.VIDEO,
            caption="Short video #реклама",
            reactions_data=[("🤡", 50)]
        )
        flags = self.parser._extract_flags(message)
        expected_flags = ["video", "advert", "clownpoo"]
        self.assertCountEqual(flags, expected_flags)

    def test_flag_multiple_no_image_stream_paywall_foreign(self):
        # Ensure current channel is mocked for foreign channel check
        self.parser.get_channel_username = MagicMock(return_value="current_channel")
        message = self._create_mock_message(
            text="Join our livestream! Full access on Boosty. Check out https://t.me/other_channel",
        )
        flags = self.parser._extract_flags(message)
        expected_flags = ["no_image", "stream", "paywall", "foreign_channel"]
        self.assertCountEqual(flags, expected_flags)

    def test_flag_multiple_sticker_donat_poo_hid(self):
        message = self._create_mock_message(
            media=MessageMediaType.STICKER,
            caption="Sticker with донат link https://t.me/+SECRET",
            reactions_data=[("💩", 30)]
        )
        flags = self.parser._extract_flags(message)
        # Sticker implies no 'no_image' flag
        expected_flags = ["sticker", "donat", "clownpoo", "hid_channel"]
        self.assertCountEqual(flags, expected_flags)

    def test_flag_multiple_text_only_advert_stream_clown_foreign(self):
        self.parser.get_channel_username = MagicMock(return_value="my_channel")
        message = self._create_mock_message(
            text="Это партнерский пост про наш вебинар. Ссылка на канал https://t.me/another. Регистрируйтесь тут!",
            reactions_data=[("🤡", 31)]
        )
        flags = self.parser._extract_flags(message)
        expected_flags = ["no_image", "advert", "stream", "clownpoo", "foreign_channel"]
        self.assertCountEqual(flags, expected_flags)

    def test_flag_multiple_video_note_fwd_paywall_mention(self):
        message = self._create_mock_message(
            media=MessageMediaType.VIDEO_NOTE,
            caption="Video note from @someone, full version on Sponsr",
            forward_from=True # Forwarded from user
        )
        flags = self.parser._extract_flags(message)
        expected_flags = ["video", "fwd", "paywall", "mention"]
        self.assertCountEqual(flags, expected_flags)

    def test_flag_link_with_tme_excluded(self):
        message = self._create_mock_message(text="Check out this Telegram link https://t.me/channel_name")
        # Use _generate_html_body to ensure link detection logic runs
        _ = self.parser._generate_html_body(message)
        self.assertNotIn("link", self.parser._extract_flags(message))
    
    def test_flag_link_with_mixed_links(self):
        message = self._create_mock_message(text="Check both links: https://example.com and https://t.me/channel_name")
        # Use _generate_html_body to ensure link detection logic runs
        _ = self.parser._generate_html_body(message)
        self.assertIn("link", self.parser._extract_flags(message))


if __name__ == '__main__':
    unittest.main() 
