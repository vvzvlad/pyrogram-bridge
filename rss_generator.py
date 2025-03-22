import logging
from datetime import datetime, timezone
from typing import Optional
from feedgen.feed import FeedGenerator
from post_parser import PostParser
from config import get_settings
import re

Config = get_settings()

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
    logger.addHandler(handler)


async def _create_time_based_media_groups(messages, merge_seconds: int = 5):
    """
    Create media groups based on time difference between messages
    """
    messages_sorted = sorted(messages, key=lambda x: x.date)
    cluster = []
    last_msg_date = None
    current_media_group_id = None

    for msg in messages_sorted:
        
        if not cluster:
            cluster.append(msg)
            last_msg_date = msg.date
            current_media_group_id = getattr(msg, "media_group_id", None)
            continue
        
        time_diff = (msg.date - last_msg_date).total_seconds()
        
        msg_media_group_id = getattr(msg, "media_group_id", None)
        
        if time_diff <= merge_seconds:
            if current_media_group_id:
                msg.media_group_id = current_media_group_id
            elif msg_media_group_id:
                current_media_group_id = msg_media_group_id
                for m in cluster:
                    m.media_group_id = current_media_group_id
            cluster.append(msg)
            last_msg_date = msg.date
        else:
            if len(cluster) >= 2 and not current_media_group_id:
                new_group_id = f"time_{min(m.date for m in cluster)}"
                for m in cluster:
                    m.media_group_id = new_group_id
            cluster = [msg]
            last_msg_date = msg.date
            current_media_group_id = msg_media_group_id
    
    if len(cluster) >= 2 and not current_media_group_id:
        new_group_id = f"time_{min(m.date for m in cluster)}"
        for m in cluster:
            m.media_group_id = new_group_id

    return messages

async def _create_messages_groups(messages):
    """
    Process messages into formatted posts, handling media groups
    """
    processing_groups = []
    media_groups = {}
    
    # First pass - collect messages and organize into processing groups
    for message in messages:
        try:
            # Skip service messages about pinned posts and new chat photos
            if message.service:
                if 'PINNED_MESSAGE' in str(message.service): continue
                if 'NEW_CHAT_PHOTO' in str(message.service): continue
                
            if message.media_group_id:
                if message.media_group_id not in media_groups:
                    media_groups[message.media_group_id] = []
                media_groups[message.media_group_id].append(message)
            else:
                processing_groups.append([message]) # Single message becomes its own processing group
                
        except Exception as e:
            logger.error(f"_create_messages_groups: channel {message.chat.username}, message_id {message.id}, error {str(e)}")
            continue
    
    # Sort messages within media groups by message ID in descending order
    for media_group in media_groups.values():
        media_group.sort(key=lambda x: x.id, reverse=False)
        processing_groups.append(media_group)
    
    # Sort processing groups by date of first message in each group
    processing_groups.sort( key=lambda group: group[0].date, reverse=True )
    
    return processing_groups

async def _trim_messages_groups(messages_groups, limit):
    """
    Trim messages groups to limit
    """
    if messages_groups: # Remove the oldest group (the one with the lowest message id based on the first message's date)
        messages_groups.pop()
    
    if len(messages_groups) > limit: # Trim groups if they exceed the specified limit
        messages_groups = messages_groups[:limit]
    
    return messages_groups

async def _render_messages_groups(messages_groups, post_parser, exclude_flags: str | None = None, exclude_text: str | None = None):
    """
    Render message groups into HTML format
    Args:
        messages_groups: List of message groups (each group is a list of messages)
        post_parser: PostParser instance
        exclude_flags: Comma-separated list of flags to exclude
        exclude_text: Text to exclude from posts (comma-separated phrases)
    Returns:
        List of rendered posts
    """
    rendered_posts = []
    
    for group in messages_groups:
        try:
            if len(group) == 1: # Single message - simple case
                message_data = post_parser.process_message(group[0])
                html_parts = [
                    f'<div class="message-header">{message_data["html"]["header"]}</div>',
                    f'<div class="message-media">{message_data["html"]["media"]}</div>',
                    f'<div class="message-text">{message_data["html"]["body"]}</div>',
                    f'<div class="message-footer">{message_data["html"]["footer"]}</div>'
                ]
                rendered_posts.append({
                    'html': '\n'.join(html_parts),
                    'date': message_data['date'],
                    'message_id': message_data['message_id'],
                    'title': message_data['html']['title'],
                    'text': message_data['text'],
                    'author': message_data['author'],
                    'flags': message_data['flags']
                })
            else: # Multiple messages in group - merge media, text and html body
                processed_messages = [post_parser.process_message(msg) for msg in group]
                main_message = next(  # Determine main message for header/footer/title
                    (msg for msg in processed_messages if msg['text']),
                    processed_messages[0]  # fallback if no message contains text
                )
                
                # Collect all media sections from all messages in the group
                all_media = [msg['html']['media'] for msg in processed_messages if msg['html']['media']]
                combined_media = '\n<br>\n'.join(all_media)

                # Merge text fields from all messages
                all_texts = [msg['text'] for msg in processed_messages if msg['text']]
                combined_text = '\n'.join(all_texts)

                # Merge html body sections from all messages
                all_html_bodies = [msg['html']['body'] for msg in processed_messages if msg['html']['body']]
                combined_html_body = '\n<br><br>\n'.join(all_html_bodies)

                html_parts = [
                    f'<div class="message-header">{main_message["html"]["header"]}</div>',
                    f'<div class="message-media">{combined_media}</div>',
                    f'<div class="message-text">{combined_html_body}</div>',
                    f'<div class="message-footer">{main_message["html"]["footer"]}</div>'
                ]

                rendered_posts.append({
                    'html': '\n'.join(html_parts),
                    'date': main_message['date'],
                    'message_id': main_message['message_id'],
                    'title': main_message['html']['title'],
                    'text': combined_text,
                    'author': main_message['author'],
                    'flags': main_message['flags']
                })
                
        except Exception as e:
            logger.error(f"message_group_rendering_error: error {str(e)}")
            continue

    # Filter posts by exclude_flags
    if exclude_flags:
        # Split comma-separated exclude_flags into a list.
        exclude_flag_list = [flag.strip() for flag in exclude_flags.split(',')]
        filtered_posts = []
        for post in rendered_posts:
            # If "all" is specified and the post has any flags, exclude the post.
            if "all" in exclude_flag_list and post['flags']:
                continue
            # Exclude post if any flag in the exclude list is present in the post's flags.
            if any(flag in post['flags'] for flag in exclude_flag_list):
                continue
            filtered_posts.append(post)
        rendered_posts = filtered_posts
    
    # Filter posts by exclude_text
    if exclude_text:
        # Compile single regex pattern
        exclude_pattern = re.compile(exclude_text.strip(), re.IGNORECASE)
        filtered_posts = []
        for post in rendered_posts:
            # Check if pattern matches the post text
            if not exclude_pattern.search(post['text']):
                filtered_posts.append(post)
            else:
                logger.debug(f"excluded_post: message_id {post['message_id']}, pattern {exclude_pattern.pattern}")
        rendered_posts = filtered_posts
    
    # Sort by date
    rendered_posts.sort(key=lambda x: x['date'], reverse=True)
    return rendered_posts

async def generate_channel_rss(channel: str, 
                                post_parser: Optional[PostParser] = None, 
                                client = None, 
                                limit: int = 20, 
                                exclude_flags: str | None = None,
                                exclude_text: str | None = None,
                                merge_seconds: int = 5
                                ) -> str:
    """
    Generate RSS feed for channel using actual messages
    Args:
        channel: Telegram channel name
        post_parser: Optional PostParser instance. If not provided, will create new one
        client: Telegram client instance
        limit: Maximum number of posts to include in the RSS feed
        exclude_flags: Flags to exclude from the RSS feed
        exclude_text: Text to exclude from posts
    Returns:
        RSS feed as string in XML format
    """
    if limit < 1:
        raise ValueError(f"limit must be positive, got {limit}")
    if limit > 200:
        raise ValueError(f"limit cannot exceed 200, got {limit}")

    try:
        if post_parser is None:
            post_parser = PostParser(client=client)
            
        fg = FeedGenerator()
        fg.load_extension('dc')
        base_url = Config['pyrogram_bridge_url']
        
        try:
            channel = post_parser.channel_name_prepare(channel)
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
        async for message in post_parser.client.get_chat_history(channel, limit=limit*2):
            messages.append(message)
            
        # Process messages into groups and render them
        if Config['time_based_merge']:
            messages = await _create_time_based_media_groups(messages, merge_seconds)
        message_groups = await _create_messages_groups(messages)
        message_groups = await _trim_messages_groups(message_groups, limit)
        final_posts = await _render_messages_groups(message_groups, post_parser, exclude_flags, exclude_text)
        
        # Generate feed entries
        for post in final_posts:
            fe = fg.add_entry()
            fe.title(post['title'])
            
            post_link = f"https://t.me/{channel_username}/{post['message_id']}"
            fe.link(href=post_link)
            
            fe.description(post['text'].replace('\n', ' '))
            fe.content(content=post['html'], type='CDATA')
            
            pub_date = datetime.fromtimestamp(post['date'], tz=timezone.utc)
            fe.pubDate(pub_date)
            fe.guid(post_link, permalink=True)
            
            if post['author'] and post['author'] != main_name:
                fe.author(name="", email=post['author'])
                
        rss_feed = fg.rss_str(pretty=True)
        if isinstance(rss_feed, bytes):
            return rss_feed.decode('utf-8')
        return rss_feed
        
    except Exception as e:
        logger.error(f"generate_channel_rss: channel {channel}, error {str(e)}")
        raise


async def generate_channel_html(channel: str, 
                                post_parser: Optional[PostParser] = None, 
                                client = None, 
                                limit: int = 20, 
                                exclude_flags: str | None = None,
                                exclude_text: str | None = None,
                                merge_seconds: int = 5
                                ) -> str:
    """
    Generate HTML feed for channel using actual messages
    Args:
        channel: Telegram channel name
        post_parser: Optional PostParser instance. If not provided, will create new one
        client: Telegram client instance
        limit: Maximum number of posts to include in the RSS feed
        exclude_flags: Flags to exclude from the RSS feed
        exclude_text: Text to exclude from posts
    Returns:
        HTML feed as string
    """
    if limit < 1:
        raise ValueError(f"limit must be positive, got {limit}")
    if limit > 200:
        raise ValueError(f"limit cannot exceed 200, got {limit}")

    try:
        if post_parser is None:
            post_parser = PostParser(client=client)
            
        base_url = Config['pyrogram_bridge_url']
        
        try:
            channel = post_parser.channel_name_prepare(channel)
            channel_info = await post_parser.client.get_chat(channel)
            channel_username = post_parser.get_channel_username(channel_info)
            if not channel_username:
                return create_error_feed(channel, base_url)
        except Exception as e:
            if "USERNAME_INVALID" in str(e) or "USERNAME_NOT_OCCUPIED" in str(e):
                return create_error_feed(channel, base_url)
            else:
                raise

        
        # Collect messages
        messages = []
        async for message in post_parser.client.get_chat_history(channel, limit=limit):
            messages.append(message)
            
        # Process messages into groups and render them
        if Config['time_based_merge']:
            messages = await _create_time_based_media_groups(messages, merge_seconds)
        message_groups = await _create_messages_groups(messages)
        message_groups = await _trim_messages_groups(message_groups, limit)
        final_posts = await _render_messages_groups(message_groups, post_parser, exclude_flags, exclude_text)

        # Generate HTML content
        html_posts = [post['html'] for post in final_posts]
        html = '\n<hr class="post-divider">\n'.join(html_posts)
        return html
        
    except Exception as e:
        logger.error(f"html_generation_error: channel {channel}, error {str(e)}")
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