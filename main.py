# invite: https://discord.com/api/oauth2/authorize?client_id=1038584511723753562
# &permissions=268453904&scope=bot%20applications.commands

from __future__ import annotations

import discord

from datetime import datetime, timezone
from functools import wraps
import os
import random
import re
import sqlite3
import string
import traceback
import typing as t

from discord import app_commands
from unique_names_generator import get_random_name
from unique_names_generator.data import ADJECTIVES, COLORS, ANIMALS


DEPLOY = False  # deploy todo: True


def console_log_with_time(msg: str, **kwargs):
    print(f'[team] {datetime.now(tz=timezone.utc):%Y/%m/%d %H:%M:%S%f%z} - {msg}', **kwargs)


class CursorCallable(t.Protocol):
    def __call__(self, db_cursor: sqlite3.Cursor = None, *args, **kwargs) -> t.Any: ...


def db_connect_wrapper(func: CursorCallable):
    """Wrapper handles opening database, creating tables if they don't exist yet, and closing database."""
    @wraps(func)
    def connect_to_db(*args, **kwargs):
        console_log_with_time(f'[{func.__name__}] Opening database...')
        con = sqlite3.connect('team/data.db')
        cur = con.cursor()

        cur.execute(
            'CREATE TABLE IF NOT EXISTS Teams '  # NB: table_number is text to allow for 1.12 etc.
            '(team_name TEXT PRIMARY KEY, leader_id INTEGER, table_number TEXT, join_code TEXT, id INTEGER)'
        )
        cur.execute(
            'CREATE TABLE IF NOT EXISTS Users '
            '(user_id INTEGER PRIMARY KEY, team_name TEXT)'
        )

        func_result = func(*args, **kwargs, db_cursor=cur)

        con.commit()
        con.close()
        console_log_with_time(f'[{func.__name__}] Database closed.')

        return func_result

    return connect_to_db


class TeamerClient(discord.Client):
    def __init__(self, guild_id: int):
        intents = discord.Intents.default()
        super().__init__(intents=intents)

        self.tree = app_commands.CommandTree(self)
        self.dev_guild_id = guild_id
        self.dev_sync_guild = discord.Object(id=self.dev_guild_id)

    async def setup_hook(self):
        if DEPLOY:
            self.tree.clear_commands(guild=self.dev_sync_guild)  # clear local commands
            await self.tree.sync(guild=self.dev_sync_guild)
            await self.tree.sync()  # global sync
            commands = await self.tree.fetch_commands()
        else:  # dev
            self.tree.copy_global_to(guild=self.dev_sync_guild)
            await self.tree.sync(guild=self.dev_sync_guild)
            commands = await self.tree.fetch_commands(guild=self.dev_sync_guild)

        console_log_with_time(f'Commands synced with {DEPLOY=}.'
                              f'{" NB: Global commands may take an hour to appear." if DEPLOY else ""}')
        for c in commands:
            console_log_with_time(f'Command ID {c.id} - "{c.name}" synced to Discord.')


client = TeamerClient(1035865794359345192)  # DurHack Discord id


@db_connect_wrapper
def gen_team_name(db_cursor: sqlite3.Cursor = None) -> str:
    def make_name():
        return get_random_name(combo=[ADJECTIVES, COLORS, ANIMALS], separator='-', style='lowercase') + 's'

    name = ''
    current_teams = set([r[0] for r in db_cursor.execute('SELECT team_name FROM Teams').fetchall()])
    while not name or name in current_teams:
        name = make_name()

    return name


@db_connect_wrapper
def on_which_team(user_id: int, db_cursor: sqlite3.Cursor = None) -> str:
    if resp := db_cursor.execute('SELECT team_name FROM Users WHERE user_id = ?', (user_id,)).fetchone():
        return resp[0]
    else:
        return ''


async def on_team(inter: discord.Interaction) -> str:
    if team_name := on_which_team(inter.user.id):
        await inter.response.send_message(f'You are already on a team: **{team_name}**.'
                                          ' Use `/leave` to leave your current team.',
                                          ephemeral=True)
        return team_name  # the team name
    else:
        return ''


@db_connect_wrapper
def create_team(team_name: str, leader_id: int, table_number: str, join_code: str, channel_id: int,
                db_cursor: sqlite3.Cursor = None):
    db_cursor.execute(
        'INSERT INTO Teams (team_name, leader_id, table_number, join_code, id) VALUES (?, ?, ?, ?, ?)',
        (team_name, leader_id, table_number, join_code, channel_id)
    )


@db_connect_wrapper
def add_user_to_team(user_id: int, team_name: str, db_cursor: sqlite3.Cursor = None):
    try:
        db_cursor.execute(
            'INSERT INTO Users (user_id, team_name) VALUES (?, ?)',
            (user_id, team_name)
        )
    except sqlite3.IntegrityError:
        # user already on a team
        raise ValueError(f'{user_id} already on a team')


@db_connect_wrapper
def make_join_code(db_cursor: sqlite3.Cursor = None) -> str:
    def make_code():
        code_parts = random.choices(list(string.ascii_uppercase), k=4) + random.choices(list(string.digits[1:]), k=3)
        random.shuffle(code_parts)
        return ''.join(code_parts)

    join_code = ''
    current_codes = set([r[0] for r in db_cursor.execute('SELECT join_code FROM Teams').fetchall()])
    while not join_code or join_code in current_codes:
        join_code = make_code()

    return join_code


async def make_team_channel(guild: discord.Guild,
                            team_name: str, channel_description: str) -> t.Tuple[discord.Role, discord.TextChannel]:

    team_role = await guild.create_role(name=team_name, colour=discord.Colour.from_str('#592275'), mentionable=True,
                                        reason='New team role created automatically by Teamer bot.')

    team_channel_cat = discord.utils.get(guild.categories, name='Team Channels')

    team_channel = await team_channel_cat.create_text_channel(
        team_name,
        topic=channel_description
    )
    await team_channel.edit(sync_permissions=True)
    perms = team_channel.overwrites
    perms[team_role] = discord.PermissionOverwrite(view_channel=True)
    await team_channel.edit(
        overwrites=perms,
        reason='Allow team members to view channel.'
    )

    return team_role, team_channel


@db_connect_wrapper
def get_current_teams(db_cursor: sqlite3.Cursor = None) -> t.List[str]:
    return [r[0] for r in db_cursor.execute('SELECT team_name FROM Teams').fetchall()]


@db_connect_wrapper
def delete_team_from_db(team_name: str, db_cursor: sqlite3.Cursor = None):
    db_cursor.execute('DELETE FROM Teams WHERE team_name = ?', (team_name, ))
    db_cursor.execute('DELETE FROM Users WHERE team_name = ?', (team_name,))


@client.event
async def on_guild_channel_delete(channel: discord.abc.GuildChannel):
    if (team_name := channel.name) in get_current_teams():
        delete_team_from_db(team_name)

        team_role: discord.Role = discord.utils.get(channel.guild.roles, name=team_name)
        await team_role.delete(reason='Channel deleted through Discord.')

        console_log_with_time(f'**{team_name}** channels and roles deleted successfully.')


@client.tree.command()
@discord.app_commands.describe(
    table_number="The table number for your team. Speak to an organiser if you don't have one."
)
async def make(inter: discord.Interaction,
               table_number: str):
    """Make a new team with you as the team leader. You can only be in one team."""

    # check if in a team
    if await on_team(inter):
        return

    team_name = gen_team_name()

    # make join code and put in channel description
    join_code = make_join_code()

    channel_description = f'This is the channel for **{team_name}**! The `/join` code is `{join_code}`. ' \
                          f'The table number is {table_number}.'

    # make team channel and role
    team_role, team_channel = await make_team_channel(inter.guild, team_name, channel_description)
    await inter.user.add_roles(team_role, reason='Added by Teamer bot via /make command.')

    create_team(team_name, inter.user.id, table_number, join_code, team_channel.id)
    add_user_to_team(inter.user.id, team_name)

    await team_channel.send(f'Hi, {inter.user.mention}!\n{channel_description}')
    await inter.response.send_message(f'Team **{team_name}** successfully created: '
                                      f'check out {team_channel.mention}!\n'
                                      f'Contact the team leader for a join code!')


@db_connect_wrapper
def resolve_join_code(join_code: str, db_cursor: sqlite3.Cursor = None) -> t.Optional[t.Tuple[str]]:
    return db_cursor.execute('SELECT team_name, id FROM Teams WHERE join_code = ?', (join_code,)).fetchone()


@client.tree.command()
@discord.app_commands.describe(
    join_code='A join code for the team you would like to join. Ask your team leader for this.'
)
async def join(inter: discord.Interaction,
               join_code: str):
    """Join the team with name team_name. You can only join one team."""

    # check if in a team
    if await on_team(inter):
        return

    # check if code valid
    if not (name_and_chan := resolve_join_code(join_code)):
        await inter.response.send_message(f'`{join_code}` is not a valid join code. '
                                          f'Please confirm your code again with the team leader.',
                                          ephemeral=True)
        return
    team_name, channel_id = name_and_chan
    team_channel: discord.TextChannel = discord.utils.get(inter.guild.channels, id=channel_id)

    # todo: check if team full

    # update db
    add_user_to_team(inter.user.id, team_name)

    # assign role
    await inter.user.add_roles(
        discord.utils.get(inter.guild.roles, name=team_name),
        reason='Add new user to team'
    )
    await inter.response.send_message(f"You've been successfully added to team **{team_name}**! "
                                      f"Head over to {team_channel.mention} to say hi 👋",
                                      ephemeral=True)
    await team_channel.send(f'{inter.user.mention} joined the team!')


@db_connect_wrapper
def get_team_leader(team_name: str, db_cursor: sqlite3.Cursor = None) -> int:
    return db_cursor.execute('SELECT leader_id FROM Teams WHERE team_name = ?', (team_name, )).fetchone()[0]


@db_connect_wrapper
def update_leader(team_name: str, leader_id: int, db_cursor: sqlite3.Cursor = None):
    db_cursor.execute('UPDATE Teams SET leader_id = ? WHERE team_name = ?', (leader_id, team_name))


@db_connect_wrapper
def update_table_num(team_name: str, table_number: str, db_cursor: sqlite3.Cursor = None):
    db_cursor.execute('UPDATE Teams SET table_number = ? WHERE team_name = ?', (table_number, team_name))


@client.tree.command()
@discord.app_commands.describe(
    new_leader='Designate a new team leader. They must already be a member of your team.',
    table_number='Update your team with a new table number.'
)
async def update(inter: discord.Interaction,
                 new_leader: t.Optional[discord.Member], table_number: t.Optional[str]):
    """Update information about your team. Only the team leaders and moderators can do this."""

    if not new_leader and not table_number:
        await inter.response.send_message('You have to provide at least one argument.', ephemeral=True)
        return

    # checks only apply to non-moderators
    if not discord.utils.get(inter.user.roles, name='Moderator'):
        # check if on a team
        if not (team_name := on_which_team(inter.user.id)):
            await inter.response.send_message('You are not currently on a team.', ephemeral=True)
            return

        # check team leader is executing command
        if (leader_id := get_team_leader(team_name)) != inter.user.id:
            await inter.response.send_message('You are not the currently the leader of your team.', ephemeral=True)
            return
    else:
        console_log_with_time('Moderator using permissions to edit team info.')
        team_name = inter.channel.name
        if team_name not in get_current_teams():
            await inter.response.send_message(
                'As a moderator, you have to use this command within a team channel.', ephemeral=True
            )
            return

        leader_id = get_team_leader(team_name)

    team_channel: discord.TextChannel = discord.utils.get(inter.guild.channels, name=team_name)
    response_msg = ''

    # check new leader is already on the team
    if new_leader:
        if on_which_team(new_leader.id) != on_which_team(leader_id):
            await inter.response.send_message(
                'The new leader must be an existing member of this team.',
                ephemeral=True
            )
            return
        if inter.user.id == new_leader.id:
            await inter.response.send_message(f"You are already the leader of this team. 😆", ephemeral=True)
            return

        update_leader(team_name, new_leader.id)
        response_msg += f'{new_leader.mention} is the new leader of **{team_name}**! 🎉\n'

    if table_number:
        update_table_num(team_name, table_number)
        new_description = re.sub(r'table number is .+\.$', f'table number is {table_number}.', team_channel.topic)
        await team_channel.edit(topic=new_description)
        response_msg += f"**{team_name}**'s new table number is **{table_number}**!"

    await inter.response.send_message(response_msg.strip('\n'))


@db_connect_wrapper
def drop_user(user_id: int, db_cursor: sqlite3.Cursor = None):
    db_cursor.execute('DELETE FROM Users WHERE user_id = ?', (user_id,))


@db_connect_wrapper
def count_members(team_name: str, db_cursor: sqlite3.Cursor = None) -> int:
    return db_cursor.execute('SELECT COUNT(*) FROM Users WHERE team_name = ?', (team_name,)).fetchone()[0]


@client.tree.command()
async def leave(inter: discord.Interaction):
    """Leaves the team you are currently on."""

    # check if in a team
    if not (team_name := on_which_team(inter.user.id)):
        await inter.response.send_message("You don't appear to be on a team. You can't leave nothing!",
                                          ephemeral=True)
        return

    # check if team leader
    if get_team_leader(team_name) == inter.user.id and count_members(team_name) > 1:
        await inter.response.send_message("You can't leave a team where you're the leader! "
                                          "Use `/update` to change the team leader.", ephemeral=True)
        return

    # remove from db
    drop_user(inter.user.id)

    team_role: discord.Role = discord.utils.get(inter.guild.roles, name=team_name)
    await inter.user.remove_roles(team_role, reason='User left team.')

    # todo: update member count

    leaving_msg = f'{inter.user.mention} left **{team_name}**.'
    await inter.response.send_message(leaving_msg)
    # doubles up message
    # team_making_channel: discord.TextChannel = discord.utils.get(inter.guild.channels, id=1035872282284937216)
    # await team_making_channel.send(leaving_msg)

    # if team is empty
    if count_members(team_name) == 0:
        delete_team_from_db(team_name)

        #   remove role (not channel)
        await team_role.delete(reason='Team was empty after `/leave` command.')

        team_channel: discord.TextChannel = discord.utils.get(inter.guild.channels, name=team_name)
        await team_channel.send('🗑️ Channel marked as archived when all members left.')
        await team_channel.edit(name=f'🗑️{team_name}')


@db_connect_wrapper
def get_table_from_db(team_name: str, db_cursor: sqlite3.Cursor = None) -> str:
    return db_cursor.execute('SELECT table_number FROM Teams WHERE team_name = ?', (team_name,)).fetchone()[0]


@client.tree.command()
async def get_table_number(inter: discord.Interaction, team_name: str):
    """Retrieves the table number for team_name from the database"""

    if team_name.lower() not in get_current_teams():
        await inter.response.send_message(
            f'`{team_name}` is not a valid team name. Make sure to include any `-`s.',
            ephemeral=True
        )
        return

    await inter.response.send_message(
        f"**{team_name}**' table number is **{get_table_from_db(team_name)}**!",
        ephemeral=True
    )


@make.error
@join.error
@update.error
@leave.error
@get_table_number.error
async def cmd_error(inter: discord.Interaction, err: discord.app_commands.AppCommandError):
    time_str = f'{datetime.now(tz=timezone.utc):%Y%m%d_%H%M%S.%f}'
    log_filepath = f'team/errors/{time_str}_traceback.log'

    with open(log_filepath, 'w+') as fobj:
        fobj.write(f'Error at {time_str}\nUser: {inter.user.id} | Channel: {inter.channel.id}\nData: {inter.data}\n')
        fobj.write(f'Error with `/{inter.command.name}` command: {err!s}\n\n')
        fobj.write(''.join(traceback.TracebackException.from_exception(err).format()))
        fobj.close()

    console_log_with_time(f'Error traceback written to {log_filepath}')

    await inter.channel.send_message(
        'Something went wrong with a command! '
        f'Contact <@141243441614028800> to find out what went wonky 😬.\n(📝 This is for him: `{time_str}`.)'
    )

    if not inter.response.is_done():
        await inter.response.send_message('An error occurred.', ephemeral=True)


@client.event
async def on_ready():
    await client.change_presence(activity=discord.Game('with DurHack teams'))

    console_log_with_time('Bot ready & running - hit me with team commands!')


client.run(os.environ['discord_team_bot_token'])
