import logging
from datetime import datetime, timezone
from typing import Optional
from feedgen.feed import FeedGenerator
from post_parser import PostParser
from config import get_settings
from bs4 import BeautifulSoup

Config = get_settings()

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
    logger.addHandler(handler)

def reorganize_post_content(html):
    """
    Reorganize post content to move text after all media
    Args:
        html: Post HTML content
    Returns:
        Reorganized HTML content
    """
    soup = BeautifulSoup(html, 'html.parser')
    
    # Find all media and text blocks
    media_blocks = soup.find_all('div', class_='message-media')
    text_blocks = soup.find_all('div', class_='message-text')
    
    if not text_blocks:  # No text to move
        return html
        
    # Create new structure
    new_content = []
    
    # Add all media first
    for media in media_blocks:
        media.extract()
        new_content.append(str(media))
        new_content.append('<br>')
        
    # Add text last
    for text in text_blocks:
        text.extract()
        new_content.append(str(text))
    
    # Add remaining elements
    remaining_elements = 0
    for element in soup.contents:
        if isinstance(element, str):
            new_content.append(element)
            remaining_elements += 1
        elif element.name != 'br' and 'message-media' not in element.get('class', []) and 'message-text' not in element.get('class', []):
            new_content.append(str(element))
            remaining_elements += 1
    
    logger.debug(f"Content reorganized with {remaining_elements} additional elements")
    return ''.join(new_content)



def merge_posts(posts):
    """Helper function to merge multiple posts into one"""
    if not posts:
        logger.debug("No posts to merge")
        return None
        
    logger.debug(f"Merging {len(posts)} posts")
    
    # Find post with most meaningful title
    main_post = posts[0]
    for post in posts:
        current_title = post.get('title', '')
        if current_title and current_title not in ['📷 Photo', '📹 Video', '📄 Document']:
            main_post = post
            logger.debug(f"Selected main post with title: {current_title}")
            break
            
    merged_post = main_post.copy()
    merged_html = []
    for post in posts:
        merged_html.append(post['html'])
    
    merged_post['html'] = '\n<br>\n'.join(merged_html)
    
    # Reorganize content after merging
    merged_post['html'] = reorganize_post_content(merged_post['html'])
    logger.debug(f"Merged {len(posts)} posts successfully")
    return merged_post

async def process_messages(messages, post_parser, channel):
    """
    Process messages into formatted posts, handling media groups
    Args:
        messages: List of raw messages
        post_parser: PostParser instance
        channel: Channel name for logging
    Returns:
        List of formatted and merged posts
    """
    posts = []
    media_groups = {}
    
    # First pass - collect messages and media groups
    for message in messages:
        try:
            # Skip service messages about pinned posts
            if message.service and 'PINNED_MESSAGE' in str(message.service):
                logger.debug(f"Skipping pinned service message {message.id} in channel {channel}")
                continue
                
            if message.media_group_id:
                if message.media_group_id not in media_groups:
                    media_groups[message.media_group_id] = []
                media_groups[message.media_group_id].append(message)
            else:
                formatted = post_parser.format_message_for_feed(
                    message,
                    top_info=True,
                    bottom_info=True
                )
                if formatted:
                    posts.append(formatted)
                
        except Exception as e:
            logger.error(f"feed_entry_error: channel {channel}, message_id {message.id}, error {str(e)}")
            continue
    
    # Process media groups
    for _group_id, group_messages in media_groups.items():
        if not group_messages:
            continue
            
        group_messages.sort(key=lambda x: x.id)
        formatted_posts = []
        
        for i, msg in enumerate(group_messages):
            formatted = post_parser.format_message_for_feed(
                msg,
                top_info=(i == 0),
                bottom_info=(i == len(group_messages) - 1)
            )
            if formatted:
                formatted_posts.append(formatted)
                
        if formatted_posts:
            posts.append(merge_posts(formatted_posts))
    
    # Sort all posts by date
    posts.sort(key=lambda x: x['date'], reverse=True)
    return posts

async def generate_channel_rss(channel: str, post_parser: Optional[PostParser] = None, client = None, limit: int = 20) -> str:
    """
    Generate RSS feed for channel using actual messages
    Args:
        channel: Telegram channel name
        post_parser: Optional PostParser instance. If not provided, will create new one
        client: Telegram client instance
        limit: Maximum number of posts to include in the RSS feed
        output_type: 'rss' or 'html'
    Returns:
        RSS feed as string in XML format
    """
    if limit < 1:
        raise ValueError(f"limit must be positive, got {limit}")
    if limit > 100:
        raise ValueError(f"limit cannot exceed 100, got {limit}")

    try:
        if post_parser is None:
            post_parser = PostParser(client=client)
            
        fg = FeedGenerator()
        fg.load_extension('dc')
        base_url = Config['pyrogram_bridge_url']
        
        try:
            channel_info = await post_parser.client.get_chat(channel)
            channel_title = channel_info.title or f"Telegram: {channel}"
            channel_username = post_parser.get_channel_username(channel_info)
            if not channel_username:
                return create_error_feed(channel, base_url)
        except Exception as e:
            if "USERNAME_INVALID" in str(e) or "USERNAME_NOT_OCCUPIED" in str(e):
                return create_error_feed(channel, base_url)
            else:
                raise

        # Set feed metadata
        main_name = f"{channel_title} (@{channel_username})"
        fg.title(main_name)
        fg.link(href=f"https://t.me/{channel_username}", rel='alternate')
        fg.description(f'Telegram channel {channel_username} RSS feed')
        fg.language('ru')
        fg.id(f"{base_url}/rss/{channel}")
        
        # Collect messages
        messages = []
        async for message in post_parser.client.get_chat_history(channel, limit=limit):
            messages.append(message)
            
        # Process messages into formatted posts
        final_posts = await process_messages(messages, post_parser, channel)
        
        # Generate feed entries
        for post in final_posts:
            fe = fg.add_entry()
            fe.title(post.get('title', 'Untitled post'))
            
            post_link = f"https://t.me/{channel_username}/{post['message_id']}"
            fe.link(href=post_link)
            
            html_content = post.get('html', '')
            text_content = post.get('text', '')
            fe.description(text_content.replace('\n', ' '))
            fe.content(content=html_content, type='CDATA')
            
            pub_date = datetime.fromtimestamp(post['date'], tz=timezone.utc)
            fe.pubDate(pub_date)
            fe.guid(post_link, permalink=True)
            
            if post.get('author') and post['author'] != main_name:
                fe.author(name="", email=post['author'])
                
        rss_feed = fg.rss_str(pretty=True)
        if isinstance(rss_feed, bytes):
            return rss_feed.decode('utf-8')
        return rss_feed
        
    except Exception as e:
        logger.error(f"rss_generation_error: channel {channel}, error {str(e)}")
        raise


async def generate_channel_html(channel: str, post_parser: Optional[PostParser] = None, client = None, limit: int = 20) -> str:
    """
    Generate HTML feed for channel using actual messages
    Args:
        channel: Telegram channel name
        post_parser: Optional PostParser instance. If not provided, will create new one
        client: Telegram client instance
        limit: Maximum number of posts to include in the RSS feed
    Returns:
        HTML feed as string
    """
    if limit < 1:
        raise ValueError(f"limit must be positive, got {limit}")
    if limit > 100:
        raise ValueError(f"limit cannot exceed 100, got {limit}")

    try:
        if post_parser is None:
            post_parser = PostParser(client=client)
            
        base_url = Config['pyrogram_bridge_url']
        
        try:
            channel_info = await post_parser.client.get_chat(channel)
            channel_title = channel_info.title or f"Telegram: {channel}"
            channel_username = post_parser.get_channel_username(channel_info)
            if not channel_username:
                return create_error_feed(channel, base_url)
        except Exception as e:
            if "USERNAME_INVALID" in str(e) or "USERNAME_NOT_OCCUPIED" in str(e):
                return create_error_feed(channel, base_url)
            else:
                raise

        # Set feed metadata
        main_name = f"{channel_title} (@{channel_username})"
        html_content = f'<h1>{main_name}</h1>'
        
        # Collect messages
        messages = []
        async for message in post_parser.client.get_chat_history(channel, limit=limit):
            messages.append(message)
            
        # Process messages into formatted posts
        final_posts = await process_messages(messages, post_parser, channel)

        html_posts = []
        for post in final_posts:
            html_content = post.get('html', '')
            if html_content:
                html_posts.append(f'<div class="telegram-post">{html_content}</div>')
        return '\n<hr class="post-divider">\n'.join(html_posts)
        
    except Exception as e:
        logger.error(f"rss_generation_error: channel {channel}, error {str(e)}")
        raise

def create_error_feed(channel: str, base_url: str) -> str:
    """
    Create an empty RSS feed with metadata indicating an error when the channel is not found.
    Args:
        channel: Telegram channel name
        base_url: Base URL for RSS feed
    Returns:
        Empty RSS feed as string in XML format.
    """
    fg = FeedGenerator()
    
    fg.title(f"Error: Channel @{channel} not found")
    fg.link(href=f"https://t.me/{channel}", rel='alternate')
    fg.description(f'Error: Telegram channel @{channel} does not exist')
    fg.language('en')
    fg.id(f"{base_url}/rss/{channel}")
    
    fe = fg.add_entry()
    fe.title("Channel not found")
    fe.link(href=f"https://t.me/{channel}")
    error_html = f"<p>The Telegram channel @{channel} does not exist or is not accessible.</p>"
    fe.description(f"{error_html}")
    fe.content(content=error_html, type='CDATA')
    fe.pubDate(datetime.now(tz=timezone.utc))
    fe.guid(f"https://t.me/{channel}", permalink=True)

    rss_feed = fg.rss_str(pretty=True)
    if isinstance(rss_feed, bytes):
        return rss_feed.decode('utf-8')
    return rss_feed