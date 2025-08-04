import discord
from discord.ext import commands, tasks
from datetime import datetime, timedelta, time, timezone
from zoneinfo import ZoneInfo # タイムゾーンのために追加
import asyncio
from collections import defaultdict, deque
from openai import AsyncOpenAI
import os
from dotenv import load_dotenv
import psutil
import platform
import gc

# .envファイルから環境変数を読み込み
load_dotenv()

# 環境変数から設定を読み込み
DISCORD_BOT_TOKEN = os.getenv('DISCORD_BOT_TOKEN')
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')

# 環境変数が設定されているか確認
if not DISCORD_BOT_TOKEN:
    raise ValueError("DISCORD_BOT_TOKENが設定されていません。.envファイルを確認してください。")
if not OPENAI_API_KEY:
    raise ValueError("OPENAI_API_KEYが設定されていません。.envファイルを確認してください。")

# OpenAI クライアントの作成
openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)

# Botの設定
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True

bot = commands.Bot(command_prefix='!', intents=intents)

# --- 修正点 1: 日本時間のタイムゾーンを定義 ---
JST = ZoneInfo("Asia/Tokyo")

# 設定項目
MAX_MESSAGES_PER_SUMMARY = int(os.getenv('MAX_MESSAGES_PER_SUMMARY', 100))
BOT_CHANNEL_NAME = os.getenv('BOT_CHANNEL_NAME', '🎀サマリちゃん🎀')
API_TIMEOUT = int(os.getenv('API_TIMEOUT', 60))
API_RETRY_COUNT = int(os.getenv('API_RETRY_COUNT', 2))
PARALLEL_SUMMARY = os.getenv('PARALLEL_SUMMARY', 'true').lower() == 'true'

# 使用するモデル
MODEL_NAME = os.getenv('OPENAI_MODEL', 'gpt-4o-mini')

# --- 修正点 2: 要約スケジュールをJSTで直接定義 ---
SUMMARY_SCHEDULE = [
    # JST 6:00 (前回の18時から12時間分)
    {"hour": 6, "minute": 0, "hours_back": 12, "description": "夜間〜早朝の要約", "color": discord.Color.purple()},
    # JST 12:00 (前回の6時から6時間分)
    {"hour": 12, "minute": 0, "hours_back": 6, "description": "午前の要約", "color": discord.Color.blue()},
    # JST 18:00 (前回の12時から6時間分)
    {"hour": 18, "minute": 0, "hours_back": 6, "description": "午後の要約", "color": discord.Color.orange()},
]

# 週次サマリーのスケジュール（月曜日の朝6時）
WEEKLY_SUMMARY_SCHEDULE = {
    "weekday": 0,  # 月曜日
    "hour": 6,     # JST 6:00
    "minute": 0,
    "hours_back": 168,  # 1週間 = 168時間
    "description": "週次要約",
    "color": discord.Color.green()
}

# サーバーごとの設定を保存
server_configs = {}

# メッセージを保存する辞書
message_buffers = defaultdict(lambda: defaultdict(lambda: deque()))

# API使用量追跡用
daily_api_calls = 0
last_reset_date = datetime.now(JST).date()

class MessageData:
    def __init__(self, message):
        self.author = message.author.display_name
        self.content = message.content
        self.timestamp = message.created_at  # これはUTCのawareオブジェクト
        self.jump_url = message.jump_url
        self.channel_name = message.channel.name
        self.channel_id = message.channel.id
        self.attachments = len(message.attachments)
        self.embeds = len(message.embeds)

# --- 修正点 3: タイムスタンプ比較を堅牢化 ---
def get_messages_in_timerange(guild_id, hours_back):
    """指定時間内のメッセージを取得"""
    # 比較はUTCで行うのが安全
    cutoff_time = datetime.now(timezone.utc) - timedelta(hours=hours_back)
    messages_by_channel = {}

    for channel_id, messages in message_buffers[guild_id].items():
        # message.created_at は aware な UTC オブジェクトなので、直接比較する
        filtered_messages = [
            msg for msg in messages if msg.timestamp > cutoff_time
        ]
        if filtered_messages:
            channel_name = filtered_messages[0].channel_name
            messages_by_channel[channel_name] = filtered_messages

    return messages_by_channel

def cleanup_old_messages():
    """1週間以上前のメッセージを削除"""
    cutoff_time = datetime.now(timezone.utc) - timedelta(hours=168)

    for guild_id in message_buffers:
        for channel_id in message_buffers[guild_id]:
            # aware な datetime オブジェクト同士で比較
            while (message_buffers[guild_id][channel_id] and
                   message_buffers[guild_id][channel_id][0].timestamp < cutoff_time):
                message_buffers[guild_id][channel_id].popleft()

def generate_simple_summary(messages_by_channel):
    """OpenAI APIが使えない場合の簡易要約"""
    summaries = []
    for channel_name, messages in messages_by_channel.items():
        content_words = defaultdict(int)
        for msg in messages:
            words = msg.content.lower().split()
            for word in words:
                if len(word) > 4:
                    content_words[word] += 1
        top_words = sorted(content_words.items(), key=lambda x: x[1], reverse=True)[:3]
        if top_words:
            keywords = ", ".join([word for word, _ in top_words])
            summaries.append(f"**#{channel_name}**: {keywords}")

    if summaries:
        return "\n".join(summaries)
    return "特定のトピックは見つかりませんでした。"

async def summarize_all_channels_async(messages_by_channel, is_weekly=False, guild_name="Unknown Server"):
    """全チャンネルのメッセージを統合して要約する関数（非同期版）"""
    global daily_api_calls, last_reset_date

    # 日付が変わったらAPI使用量をリセット (JST基準)
    if datetime.now(JST).date() != last_reset_date:
        daily_api_calls = 0
        last_reset_date = datetime.now(JST).date()

    if not any(messages_by_channel.values()):
        return "要約するメッセージがありません。"

    try:
        all_conversations = []
        for channel_name, messages in messages_by_channel.items():
            if not messages:
                continue
            channel_text = f"\n=== #{channel_name} ===\n"
            message_texts = []
            max_messages = MAX_MESSAGES_PER_SUMMARY * 2 if is_weekly else MAX_MESSAGES_PER_SUMMARY
            for msg in messages[-max_messages:]:
                text = f"{msg.author}: {msg.content}"
                if msg.attachments > 0:
                    text += f" [添付ファイル: {msg.attachments}件]"
                if msg.embeds > 0:
                    text += f" [Embed: {msg.embeds}件]"
                message_texts.append(text)
            channel_text += "\n".join(message_texts)
            all_conversations.append(channel_text)

        full_conversation = "\n\n".join(all_conversations)

        # プロンプトは変更なし（内容は普遍的なため）
        if is_weekly:
            system_prompt = f"""あなたは'{guild_name}'サーバーのDiscordチャットログを要約する専門家です。
1週間分の活動を俯瞰的に分析し、簡潔で読みやすい要約を作成してください。"""
            user_prompt = f"""以下は1週間分のDiscordチャンネルの会話です。

{full_conversation}

重要な指示：
- 1週間の活動を総括的に要約
- 主要なトピック、決定事項、進捗状況を整理
- チャンネルごとの活動傾向を分析
- 重要な出来事や特筆すべき議論を強調
- 週の前半と後半での変化があれば言及
- 簡潔で読みやすい要約（1800文字以内）
- 箇条書きや見出しを活用して構造化
- 登場する人物のDisplay Nameには敬称として「さん」を付けてください

安全性に関する指示：
- 不適切、暴力的、差別的な内容が含まれる会話は、その部分を除外または一般化して要約してください
- センシティブな話題は建設的な側面のみを抽出してください
- 個人攻撃や中傷的な内容は無視してください
- 全体的にポジティブで建設的な要約を心がけてください"""
        else:
            system_prompt = f"""あなたは'{guild_name}'サーバーのDiscordチャットログを要約する専門家です。
全チャンネルを俯瞰して統合的な要約を作成してください。"""
            user_prompt = f"""以下のDiscordチャンネルの会話を要約してください。

{full_conversation}

重要な指示：
- 全チャンネルを俯瞰して統合的に要約する
- 「#チャンネル名で誰が何を話したか」を明確に記載
- 重要な情報、決定事項、注目すべきトピックを優先
- 簡潔で読みやすい要約（1800文字以内）
- 余分な前置きや説明は一切不要
- 箇条書きや見出しを活用して構造化
- 登場する人物のDisplay Nameには敬称として「さん」を付けてください

安全性に関する指示：
- 不適切、暴力的、差別的な内容が含まれる会話は、その部分を除外または一般化して要約してください
- センシティブな話題は建設的な側面のみを抽出してください
- 個人攻撃や中傷的な内容は無視してください
- 全体的にポジティブで建設的な要約を心がけてください"""

        response = await asyncio.wait_for(
            openai_client.chat.completions.create(
                model=MODEL_NAME,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                temperature=0.3,
                max_tokens=3000,
            ),
            timeout=API_TIMEOUT
        )
        daily_api_calls += 1
        if response.choices and response.choices[0].message.content:
            return response.choices[0].message.content
        else:
            return "要約の生成に失敗しました。"
    except asyncio.TimeoutError:
        print(f"OpenAI API タイムアウト: {API_TIMEOUT}秒を超えました")
        return generate_simple_summary(messages_by_channel)
    except Exception as e:
        print(f"OpenAI API エラー: {e}")
        return generate_simple_summary(messages_by_channel)

async def get_or_create_bot_channel(guild):
    for channel in guild.text_channels:
        if channel.name == BOT_CHANNEL_NAME:
            return channel
    try:
        return await guild.create_text_channel(
            name=BOT_CHANNEL_NAME,
            topic="このチャンネルはBotが定期的に要約を投稿します。"
        )
    except discord.Forbidden:
        print(f"チャンネル作成権限がありません: {guild.name}")
        return None

async def create_server_summary_embed(guild, messages_by_channel, time_description, color=discord.Color.blue(), is_weekly=False):
    # タイムスタンプはUTCが標準のため変更なし
    embed = discord.Embed(
        title=f"📋 {time_description}",
        color=color,
        timestamp=datetime.now(timezone.utc)
    )
    total_messages = sum(len(messages) for messages in messages_by_channel.values())
    active_channels = len([ch for ch, msgs in messages_by_channel.items() if msgs])
    all_authors = {msg.author for messages in messages_by_channel.values() for msg in messages}
    stats_text = f"💬 {total_messages}件 | 📍 {active_channels}ch | 👥 {len(all_authors)}人"
    embed.add_field(name="📊 統計", value=stats_text, inline=False)

    if active_channels > 0:
        channel_stats = []
        top_count = 5 if is_weekly else 3
        sorted_channels = sorted(messages_by_channel.items(), key=lambda x: len(x[1]), reverse=True)
        for channel_name, messages in sorted_channels[:top_count]:
            if messages:
                channel_stats.append(f"**#{channel_name}**: {len(messages)}件")
        if channel_stats:
            embed.add_field(name="🔥 活発なチャンネル", value=" / ".join(channel_stats), inline=False)

    summary = await summarize_all_channels_async(messages_by_channel, is_weekly=is_weekly, guild_name=guild.name)
    embed.description = summary
    return embed

async def setup_guild(guild):
    guild_id = guild.id
    bot_channel = await get_or_create_bot_channel(guild)
    server_configs[guild_id] = {
        'summary_channel': bot_channel,
        'enabled': True
    }
    if bot_channel:
        print(f"サーバー '{guild.name}' の設定完了。要約チャンネル: #{bot_channel.name}")
    else:
        print(f"サーバー '{guild.name}' でチャンネル作成に失敗しました。")

async def post_scheduled_summary(schedule_info, is_weekly=False):
    async def process_guild(guild_id, config):
        if not config.get('enabled') or not config.get('summary_channel'):
            return
        guild = bot.get_guild(guild_id)
        if not guild:
            return
        messages_by_channel = get_messages_in_timerange(guild_id, schedule_info['hours_back'])
        if messages_by_channel:
            try:
                print(f"[{datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S')}] {guild.name}: {schedule_info['description']}の生成開始")
                embed = await create_server_summary_embed(
                    guild, messages_by_channel, schedule_info['description'],
                    schedule_info['color'], is_weekly=is_weekly
                )
                summary_channel = config['summary_channel']
                if summary_channel:
                    await summary_channel.send(embed=embed)
                    total_msg = sum(len(msgs) for msgs in messages_by_channel.values())
                    print(f"[{datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S')}] {guild.name} の{schedule_info['description']}を投稿しました（{total_msg}件）")
            except discord.Forbidden:
                print(f"権限エラー ({guild.name}): チャンネルへの投稿権限がありません")
            except Exception as e:
                print(f"要約エラー ({guild.name}): {e}")
        else:
            print(f"[{datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S')}] {guild.name}: {schedule_info['description']}に新しいメッセージがないため要約をスキップ")

    tasks_to_run = [process_guild(gid, conf) for gid, conf in server_configs.items()]
    if tasks_to_run:
        if PARALLEL_SUMMARY:
            print(f"[{datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S')}] {len(tasks_to_run)}個のサーバーで並列要約開始")
            await asyncio.gather(*tasks_to_run)
        else:
            for task in tasks_to_run:
                await task

@bot.event
async def on_ready():
    bot.start_time = datetime.now(JST)
    print(f'{bot.user} がログインしました！')
    print(f'使用モデル: {MODEL_NAME}')
    print(f'要約スケジュール (JST): 6時, 12時, 18時, 月曜6時(週次)')
    print(f'メッセージ保持期間: 1週間（168時間）')
    print(f'並列処理: {"有効" if PARALLEL_SUMMARY else "無効"}')

    for guild in bot.guilds:
        await setup_guild(guild)

    scheduled_summary_task.start()
    cleanup_task.start()

@bot.event
async def on_guild_join(guild):
    print(f"新しいサーバーに参加しました: {guild.name}")
    await setup_guild(guild)

@bot.event
async def on_guild_remove(guild):
    guild_id = guild.id
    if guild_id in server_configs:
        del server_configs[guild_id]
    if guild_id in message_buffers:
        del message_buffers[guild_id]
    print(f"サーバーから削除されました: {guild.name}")

@bot.event
async def on_message(message):
    if message.author.bot or not message.guild or message.channel.name == BOT_CHANNEL_NAME:
        return

    guild_id = message.guild.id
    channel_id = message.channel.id
    message_buffers[guild_id][channel_id].append(MessageData(message))

    await bot.process_commands(message)

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    print(f"コマンドエラー: {error}")

# --- 修正点 4: スケジューラを JST で動作させる ---
@tasks.loop(minutes=1)
async def scheduled_summary_task():
    """1分ごとにスケジュールをチェックして要約を投稿 (JST基準)"""
    now_jst = datetime.now(JST)
    current_time = time(now_jst.hour, now_jst.minute)

    # 通常の要約スケジュール
    for schedule in SUMMARY_SCHEDULE:
        scheduled_time = time(schedule['hour'], schedule['minute'])
        if current_time == scheduled_time:
            await post_scheduled_summary(schedule)

    # 週次サマリーのチェック
    weekly_schedule = WEEKLY_SUMMARY_SCHEDULE
    if (now_jst.weekday() == weekly_schedule['weekday'] and
        current_time.hour == weekly_schedule['hour'] and
        current_time.minute == weekly_schedule['minute']):
        await post_scheduled_summary(weekly_schedule, is_weekly=True)
        # 週次要約の後にクリーンアップを実行
        cleanup_old_messages()

@tasks.loop(hours=6)
async def cleanup_task():
    """定期的なメモリクリーンアップ"""
    for guild_id in list(message_buffers.keys()):
        if bot.get_guild(guild_id) is None:
            del message_buffers[guild_id]
            if guild_id in server_configs:
                del server_configs[guild_id]
    cleanup_old_messages()
    gc.collect()
    print(f"[{datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S')}] メモリクリーンアップ完了")

@bot.command(name='summary')
async def manual_summary(ctx, hours: int = 24):
    """手動で現在のサーバーの要約を生成するコマンド"""
    if not ctx.guild:
        return await ctx.send("このコマンドはサーバー内でのみ使用できます。")
    hours = max(1, min(hours, 168))
    messages_by_channel = get_messages_in_timerange(ctx.guild.id, hours)
    if not messages_by_channel:
        return await ctx.send(f"過去{hours}時間の要約するメッセージがありません。")

    color = discord.Color.gold()
    if hours <= 6: color = discord.Color.green()
    elif hours <= 24: color = discord.Color.blue()
    elif hours <= 48: color = discord.Color.purple()

    print(f"[{datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S')}] {ctx.guild.name}: 手動要約を生成中（過去{hours}時間）")
    embed = await create_server_summary_embed(
        ctx.guild, messages_by_channel, f"過去{hours}時間の要約", color, is_weekly=(hours >= 168)
    )
    await ctx.send(embed=embed)

# --- 修正点 5: `status` コマンドの次回要約時刻表示を改善 ---
@bot.command(name='status')
async def bot_status(ctx):
    """Botの状態を表示"""
    if not ctx.guild:
        return await ctx.send("このコマンドはサーバー内でのみ使用できます。")
    
    guild_id = ctx.guild.id
    config = server_configs.get(guild_id, {})
    embed = discord.Embed(title="Bot Status", color=discord.Color.blue())
    summary_ch = config.get('summary_channel')
    embed.add_field(name="要約チャンネル", value=summary_ch.mention if summary_ch else "未設定", inline=False)

    active_channels = []
    total_buffered = 0
    if guild_id in message_buffers:
        for channel_id, messages in message_buffers[guild_id].items():
            if messages:
                channel = ctx.guild.get_channel(channel_id)
                if channel:
                    active_channels.append(f"#{channel.name}: {len(messages)}件")
                    total_buffered += len(messages)
    
    if active_channels:
        embed.add_field(name="監視中のチャンネル", value="\n".join(active_channels[:10]), inline=False)
        if len(active_channels) > 10:
            embed.set_footer(text=f"...他 {len(active_channels) - 10} チャンネル")

    embed.add_field(name="バッファ内のメッセージ数", value=f"合計 {total_buffered} 件", inline=True)

    # 次回の要約時刻を計算 (JST)
    now_jst = datetime.now(JST)
    all_scheduled_times = []

    # 通常の要約
    for schedule in SUMMARY_SCHEDULE:
        scheduled_time = now_jst.replace(hour=schedule['hour'], minute=schedule['minute'], second=0, microsecond=0)
        if scheduled_time < now_jst:
            scheduled_time += timedelta(days=1)
        all_scheduled_times.append((scheduled_time, schedule['description']))

    # 週次要約
    ws = WEEKLY_SUMMARY_SCHEDULE
    days_until_target = (ws['weekday'] - now_jst.weekday() + 7) % 7
    next_weekly_run = now_jst.replace(hour=ws['hour'], minute=ws['minute'], second=0, microsecond=0) + timedelta(days=days_until_target)
    if next_weekly_run < now_jst:
        next_weekly_run += timedelta(weeks=1)
    all_scheduled_times.append((next_weekly_run, ws['description']))
    
    # 直近のものを表示
    all_scheduled_times.sort()
    next_run_time, next_run_desc = all_scheduled_times[0]
    
    embed.add_field(name="次回の要約 (JST)", value=f"{next_run_time.strftime('%Y-%m-%d %H:%M')} - {next_run_desc}", inline=False)
    
    embed.add_field(name="AI要約モデル", value=f"{MODEL_NAME}", inline=True)

    if hasattr(bot, 'start_time'):
        uptime = datetime.now(JST) - bot.start_time
        days, rem = divmod(uptime.total_seconds(), 86400)
        hours, rem = divmod(rem, 3600)
        embed.add_field(name="稼働時間", value=f"{int(days)}日 {int(hours)}時間", inline=True)

    await ctx.send(embed=embed)


# 以下、変更のないコマンド群 (toggle_summary, set_summary_channel, api_usage, system, check_permissions)
# これらは時刻に直接依存しないため、変更は不要です。
# ただし、可読性のために print 文の時刻表示だけ JST に合わせる修正を加えています。

@bot.command(name='toggle_summary')
@commands.has_permissions(administrator=True)
async def toggle_summary(ctx):
    """このサーバーの要約機能のON/OFF切り替え"""
    if not ctx.guild: return
    guild_id = ctx.guild.id
    if guild_id in server_configs:
        server_configs[guild_id]['enabled'] = not server_configs[guild_id]['enabled']
        status = "有効" if server_configs[guild_id]['enabled'] else "無効"
        await ctx.send(f"要約機能を{status}にしました。")

@bot.command(name='set_summary_channel')
@commands.has_permissions(administrator=True)
async def set_summary_channel(ctx, channel: discord.TextChannel):
    """要約投稿チャンネルを設定"""
    if not ctx.guild: return
    guild_id = ctx.guild.id
    if guild_id in server_configs:
        server_configs[guild_id]['summary_channel'] = channel
        await ctx.send(f"要約チャンネルを {channel.mention} に設定しました。")

@bot.command(name='api_usage')
@commands.has_permissions(administrator=True)
async def api_usage(ctx):
    """API使用量を表示"""
    global daily_api_calls, last_reset_date
    if datetime.now(JST).date() != last_reset_date:
        daily_api_calls = 0
        last_reset_date = datetime.now(JST).date()
    embed = discord.Embed(title="📊 OpenAI API 使用状況", color=discord.Color.blue())
    embed.add_field(name="本日の使用回数 (JST基準)", value=f"{daily_api_calls}回", inline=False)
    embed.add_field(name="使用モデル", value=MODEL_NAME, inline=True)
    await ctx.send(embed=embed)

@bot.command(name='system')
@commands.has_permissions(administrator=True)
async def system_info(ctx):
    """システムリソースの使用状況を表示"""
    cpu_percent = psutil.cpu_percent(interval=1)
    memory = psutil.virtual_memory()
    process_memory = psutil.Process().memory_info().rss / 1024 / 1024
    embed = discord.Embed(title="🖥️ システム情報", color=discord.Color.green())
    embed.add_field(name="CPU", value=f"{cpu_percent}%", inline=True)
    embed.add_field(name="メモリ", value=f"{memory.percent}% ({memory.used/1024**3:.1f}/{memory.total/1024**3:.1f} GB)", inline=True)
    embed.add_field(name="Bot使用メモリ", value=f"{process_memory:.1f} MB", inline=True)
    embed.add_field(name="Python", value=platform.python_version(), inline=True)
    embed.add_field(name="参加サーバー数", value=f"{len(bot.guilds)}", inline=True)
    if hasattr(bot, 'start_time'):
        uptime = datetime.now(JST) - bot.start_time
        days = uptime.days
        embed.add_field(name="稼働時間", value=f"{days}日", inline=True)
    await ctx.send(embed=embed)

@bot.command(name='check_permissions')
@commands.has_permissions(administrator=True)
async def check_permissions(ctx):
    """Botの権限状態を確認"""
    if not ctx.guild: return
    embed = discord.Embed(title="🔒 Bot権限チェック", color=discord.Color.blue())
    me = ctx.guild.me
    required_permissions = {
        'view_channel': 'チャンネルを見る', 'send_messages': 'メッセージを送信',
        'read_message_history': 'メッセージ履歴を読む', 'manage_channels': 'チャンネルを管理',
        'embed_links': '埋め込みリンク'
    }
    global_perms = [f'{"✅" if getattr(me.guild_permissions, p, False) else "❌"} {n}' for p, n in required_permissions.items()]
    embed.add_field(name="グローバル権限", value="\n".join(global_perms), inline=False)
    summary_channel = server_configs.get(ctx.guild.id, {}).get('summary_channel')
    if summary_channel:
        perms = summary_channel.permissions_for(me)
        ch_perms = [f'{"✅" if getattr(perms, p, False) else "❌"} {n}' for p, n in required_permissions.items()]
        embed.add_field(name=f"#{summary_channel.name} の権限", value="\n".join(ch_perms), inline=False)
    await ctx.send(embed=embed)


# Botを起動
if __name__ == "__main__":
    try:
        bot.run(DISCORD_BOT_TOKEN)
    except Exception as e:
        print(f"Botの起動に失敗しました: {e}")
