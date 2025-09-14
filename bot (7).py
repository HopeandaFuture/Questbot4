import discord
from discord.ext import commands
from discord import app_commands
import sqlite3
import asyncio
from typing import Optional, Dict, List
import json
import requests
import os
import webserver

# Bot configuration
TOKEN = None  # Set this through environment variables
PREFIX = '-'

# XP Level thresholds
LEVEL_THRESHOLDS = {
    1: 0,
    2: 100,
    3: 500,
    4: 1200,
    5: 2200,
    6: 3500,
    7: 5100,
    8: 7000,
    9: 9200,
    10: 11700
}

# Bot setup - With message content intent for full functionality
# NOTE: Requires "Message Content Intent" enabled in Discord Developer Portal
intents = discord.Intents.none()
intents.guilds = True
intents.guild_messages = True
intents.guild_reactions = True
intents.message_content = True  # Privileged intent - enable in Discord Developer Portal
intents.members = True  # Privileged intent - enable in Discord Developer Portal to read member roles

bot = commands.Bot(command_prefix=PREFIX, intents=intents)

class QuestBot:
    def __init__(self):
        self.db_connection = None
        self.quest_ping_role_id = None
        self.quest_channel_id = None
        self.role_xp_assignments = {}
        self.optin_message_id = None
        self.optin_channel_id = None
        self.init_database()
    
    def init_database(self):
        """Initialize SQLite database for storing user XP and quest data"""
        self.db_connection = sqlite3.connect('quest_bot.db')
        cursor = self.db_connection.cursor()
        
        # Create users table for XP tracking
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                guild_id INTEGER,
                xp INTEGER DEFAULT 0,
                level INTEGER DEFAULT 1,
                UNIQUE(user_id, guild_id)
            )
        ''')
        
        # Create quests table for active quests
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS quests (
                message_id INTEGER PRIMARY KEY,
                guild_id INTEGER,
                channel_id INTEGER,
                title TEXT,
                content TEXT,
                completed_users TEXT DEFAULT '[]'
            )
        ''')
        
        # Create settings table for bot configuration
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS settings (
                guild_id INTEGER PRIMARY KEY,
                quest_ping_role_id INTEGER,
                quest_channel_id INTEGER,
                role_xp_assignments TEXT DEFAULT '{}'
            )
        ''')
        
        # Migrate settings table to add new columns if they don't exist
        try:
            # Check if optin_message_id column exists
            cursor.execute("PRAGMA table_info(settings)")
            columns = [row[1] for row in cursor.fetchall()]
            
            if 'optin_message_id' not in columns:
                cursor.execute('ALTER TABLE settings ADD COLUMN optin_message_id INTEGER')
                print("Added optin_message_id column to settings table")
                
            if 'optin_channel_id' not in columns:
                cursor.execute('ALTER TABLE settings ADD COLUMN optin_channel_id INTEGER')
                print("Added optin_channel_id column to settings table")
                
        except Exception as e:
            print(f"Database migration warning: {e}")
        
        # Create streak_role_gains table for tracking streak role accumulation
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS streak_role_gains (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                guild_id INTEGER,
                role_id INTEGER,
                role_name TEXT,
                xp_awarded INTEGER,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        self.db_connection.commit()
    
    def get_user_data(self, user_id: int, guild_id: int):
        """Get user XP and level data"""
        if not self.db_connection:
            return {'xp': 0, 'level': 1}
        cursor = self.db_connection.cursor()
        cursor.execute('SELECT xp, level FROM users WHERE user_id = ? AND guild_id = ?', (user_id, guild_id))
        result = cursor.fetchone()
        if result:
            return {'xp': result[0], 'level': result[1]}
        else:
            # Create new user entry
            cursor.execute('INSERT INTO users (user_id, guild_id, xp, level) VALUES (?, ?, 0, 1)', (user_id, guild_id))
            self.db_connection.commit()
            return {'xp': 0, 'level': 1}
    
    def update_user_xp(self, user_id: int, guild_id: int, xp_change: int):
        """Update user base XP and recalculate level based on total XP"""
        if not self.db_connection:
            return 0, 1
        cursor = self.db_connection.cursor()
        current_data = self.get_user_data(user_id, guild_id)
        old_level = current_data['level']
        new_base_xp = max(0, current_data['xp'] + xp_change)
        
        # Update base XP in database first
        cursor.execute('UPDATE users SET xp = ? WHERE user_id = ? AND guild_id = ?', 
                      (new_base_xp, user_id, guild_id))
        self.db_connection.commit()
        
        # Calculate level based on TOTAL XP (including roles), not just base XP
        total_xp = self.calculate_total_user_xp(user_id, guild_id)
        new_level = self.calculate_level(total_xp)
        
        # Update level in database if changed
        if old_level != new_level:
            cursor.execute('UPDATE users SET level = ? WHERE user_id = ? AND guild_id = ?', 
                          (new_level, user_id, guild_id))
            self.db_connection.commit()
            asyncio.create_task(self.update_user_level_role(user_id, guild_id, old_level, new_level))
        
        return total_xp, new_level
    
    async def create_level_roles(self, guild):
        """Create level roles if they don't exist"""
        try:
            for level in range(1, 11):
                role_name = f"Level {level}"
                # Check if role already exists
                existing_role = discord.utils.get(guild.roles, name=role_name)
                if not existing_role:
                    # Create role with a color gradient from blue to gold
                    color_value = int(0x0099ff + (0xffd700 - 0x0099ff) * (level - 1) / 9)
                    await guild.create_role(
                        name=role_name,
                        color=discord.Color(color_value),
                        reason=f"Auto-created level role for Level {level}"
                    )
                    print(f"Created role: {role_name}")
        except discord.Forbidden:
            print("Bot lacks permission to create roles")
        except Exception as e:
            print(f"Error creating level roles: {e}")
    
    async def update_user_level_role(self, user_id: int, guild_id: int, old_level: int, new_level: int):
        """Update user's level role when they level up/down"""
        try:
            guild = bot.get_guild(guild_id)
            if not guild:
                print(f"Guild {guild_id} not found")
                return
            
            member = guild.get_member(user_id)
            if not member:
                print(f"Member {user_id} not found in guild {guild_id}")
                return
            
            # Remove ALL existing level roles (not just the old one)
            removed_roles = []
            for role in member.roles:
                if role.name.startswith("Level ") and role.name != f"Level {new_level}":
                    removed_roles.append(role.name)
                    await member.remove_roles(role, reason="Level changed - removing old level role")
            
            # Add new level role
            new_role_name = f"Level {new_level}"
            new_role = discord.utils.get(guild.roles, name=new_role_name)
            if new_role:
                if new_role not in member.roles:
                    await member.add_roles(new_role, reason=f"Reached {new_role_name}")
                    print(f"✅ {member.display_name}: Removed {removed_roles} → Added {new_role_name}")
                else:
                    print(f"ℹ️ {member.display_name}: Already has {new_role_name}, removed {removed_roles}")
            else:
                # Create the role if it doesn't exist
                print(f"Creating missing level roles...")
                await self.create_level_roles(guild)
                new_role = discord.utils.get(guild.roles, name=new_role_name)
                if new_role:
                    await member.add_roles(new_role, reason=f"Reached {new_role_name}")
                    print(f"✅ {member.display_name}: Created and added {new_role_name}")
                else:
                    print(f"❌ Failed to create {new_role_name}")
        except discord.Forbidden as e:
            print(f"❌ Bot lacks permission to manage roles: {e}")
            print(f"   Make sure bot role is higher than Level roles in server settings!")
        except Exception as e:
            print(f"❌ Error updating user level role: {e}")
    
    def calculate_level(self, xp: int) -> int:
        """Calculate level based on XP"""
        for level in range(10, 0, -1):
            if xp >= LEVEL_THRESHOLDS[level]:
                return level
        return 1
    
    def calculate_total_user_xp(self, user_id: int, guild_id: int) -> int:
        """Calculate total XP including quest XP + role-based XP"""
        try:
            guild = bot.get_guild(guild_id)
            if not guild:
                print(f"Guild {guild_id} not found")
                user_data = self.get_user_data(user_id, guild_id)
                return user_data.get('xp', 0)
            
            member = guild.get_member(user_id)
            if not member:
                print(f"Member {user_id} not found in guild {guild_id}")
                user_data = self.get_user_data(user_id, guild_id)
                return user_data.get('xp', 0)
            
            # Get base XP from database (quest completions and manual additions)
            user_data = self.get_user_data(user_id, guild_id)
            base_xp = user_data.get('xp', 0)
            
            # Add XP from custom assigned roles (excluding streak roles which use accumulated system)
            custom_role_xp = 0
            for role in member.roles:
                # Skip level roles - they don't contribute to XP calculation to avoid circular dependency
                if role.name.startswith("Level "):
                    continue
                    
                role_xp_data = self.get_role_xp_and_type(guild_id, str(role.id))
                if role_xp_data:
                    xp_amount, role_type = role_xp_data
                    # Skip streak roles since they now use accumulated XP system
                    if role_type != "streak":
                        custom_role_xp += xp_amount
            
            # Add XP from accumulated streak roles (historical gains)
            accumulated_streak_xp = self.get_accumulated_streak_xp(user_id, guild_id)
            
            # Add XP from current badge roles (only for unassigned roles that have "badge" in name)
            auto_role_xp = 0
            badge_roles_found = []
            for role in member.roles:
                # Skip level roles - they don't contribute to XP calculation
                if role.name.startswith("Level "):
                    continue
                    
                role_name_lower = role.name.lower()
                role_id_str = str(role.id)
                role_xp_data = self.get_role_xp_and_type(guild_id, role_id_str)
                # Only apply auto-detection fallback if role doesn't have explicit assignment
                if not role_xp_data:
                    # Badge roles give 5 XP each (fallback for unassigned roles)
                    if "badge" in role_name_lower:
                        auto_role_xp += 5
                        badge_roles_found.append(role.name)
                    # Note: Streak roles now use accumulated XP instead of current roles
            
            # Calculate total XP - sum of base XP + all role bonuses + accumulated streak XP
            # NO level role XP to avoid circular dependency in level calculation
            total_xp = base_xp + custom_role_xp + auto_role_xp + accumulated_streak_xp
            
            # Log badge roles found for debugging
            if badge_roles_found:
                print(f"Role XP for {member.display_name}: Badge roles: {badge_roles_found}, Auto XP: {auto_role_xp}")
            if accumulated_streak_xp > 0:
                print(f"Accumulated Streak XP for {member.display_name}: {accumulated_streak_xp}")
            
            return total_xp
            
        except Exception as e:
            print(f"Error calculating total XP for user {user_id}: {e}")
            import traceback
            traceback.print_exc()
            # Fall back to database XP
            user_data = self.get_user_data(user_id, guild_id)
            return user_data.get('xp', 0)
    
    def get_leaderboard(self, guild_id: int, limit: int = 10):
        """Get top users for leaderboard with total XP including roles (opted-in users only)"""
        if not self.db_connection:
            return []
        cursor = self.db_connection.cursor()
        cursor.execute('SELECT user_id, xp, level FROM users WHERE guild_id = ? ORDER BY xp DESC', (guild_id,))
        all_users = cursor.fetchall()
        
        # Calculate total XP for each user (including role bonuses) and filter for opted-in users only
        users_with_total_xp = []
        for user_id, base_xp, level in all_users:
            # Only include users who are opted into the bot
            if self.is_user_opted_in(user_id, guild_id):
                total_xp = self.calculate_total_user_xp(user_id, guild_id)
                new_level = self.calculate_level(total_xp)
                users_with_total_xp.append((user_id, total_xp, new_level))
        
        # Sort by total XP and limit results
        users_with_total_xp.sort(key=lambda x: x[1], reverse=True)
        return users_with_total_xp[:limit]
    
    def save_settings(self, guild_id: int):
        """Save bot settings to database"""
        if not self.db_connection:
            return
        cursor = self.db_connection.cursor()
        role_xp_json = json.dumps(self.role_xp_assignments.get(guild_id, {}))
        cursor.execute('''
            INSERT OR REPLACE INTO settings 
            (guild_id, quest_ping_role_id, quest_channel_id, role_xp_assignments, optin_message_id, optin_channel_id) 
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (guild_id, self.quest_ping_role_id, self.quest_channel_id, role_xp_json, self.optin_message_id, self.optin_channel_id))
        self.db_connection.commit()
    
    def load_settings(self, guild_id: int):
        """Load bot settings from database"""
        if not self.db_connection:
            return
        cursor = self.db_connection.cursor()
        cursor.execute('SELECT quest_ping_role_id, quest_channel_id, role_xp_assignments, optin_message_id, optin_channel_id FROM settings WHERE guild_id = ?', (guild_id,))
        result = cursor.fetchone()
        if result:
            self.quest_ping_role_id = result[0]
            self.quest_channel_id = result[1]
            loaded_assignments = json.loads(result[2])
            self.optin_message_id = result[3] if len(result) > 3 else None
            self.optin_channel_id = result[4] if len(result) > 4 else None
            
            # Migrate old format to new format if needed
            migrated_assignments = {}
            for role_id, data in loaded_assignments.items():
                if isinstance(data, int):
                    # Old format: role_id -> xp_amount
                    # Migrate to new format: role_id -> {"xp": xp_amount, "type": "badge"}
                    # Default to "badge" for backward compatibility
                    migrated_assignments[role_id] = {"xp": data, "type": "badge"}
                else:
                    # New format: role_id -> {"xp": xp_amount, "type": "streak"|"badge"}
                    migrated_assignments[role_id] = data
            
            self.role_xp_assignments[guild_id] = migrated_assignments
    
    def record_streak_role_gain(self, user_id: int, guild_id: int, role_id: int, role_name: str, xp_awarded: int):
        """Record when a user gains a streak role for accumulation tracking"""
        if not self.db_connection:
            return
        cursor = self.db_connection.cursor()
        cursor.execute('''
            INSERT INTO streak_role_gains (user_id, guild_id, role_id, role_name, xp_awarded)
            VALUES (?, ?, ?, ?, ?)
        ''', (user_id, guild_id, role_id, role_name, xp_awarded))
        self.db_connection.commit()
        print(f"Recorded streak role gain: {role_name} (+{xp_awarded} XP) for user {user_id}")
    
    def get_accumulated_streak_xp(self, user_id: int, guild_id: int) -> int:
        """Get total accumulated streak XP from all historical role gains"""
        if not self.db_connection:
            return 0
        cursor = self.db_connection.cursor()
        cursor.execute('''
            SELECT SUM(xp_awarded) FROM streak_role_gains 
            WHERE user_id = ? AND guild_id = ?
        ''', (user_id, guild_id))
        result = cursor.fetchone()
        return result[0] if result[0] else 0
    
    def get_role_xp_and_type(self, guild_id: int, role_id: str):
        """Get XP amount and type for a role, returns (xp, type) or None if not assigned"""
        if guild_id not in self.role_xp_assignments:
            return None
        role_data = self.role_xp_assignments[guild_id].get(role_id)
        if role_data:
            return role_data["xp"], role_data["type"]
        return None
    
    def assign_role_xp(self, guild_id: int, role_id: str, xp_amount: int, role_type: str):
        """Assign XP and type to a role"""
        if guild_id not in self.role_xp_assignments:
            self.role_xp_assignments[guild_id] = {}
        self.role_xp_assignments[guild_id][role_id] = {"xp": xp_amount, "type": role_type}
    
    def is_user_opted_in(self, user_id: int, guild_id: int) -> bool:
        """Check if user is opted into the bot (has Level 1+ role)"""
        try:
            guild = bot.get_guild(guild_id)
            if not guild:
                return False
            
            member = guild.get_member(user_id)
            if not member:
                return False
            
            # Check if user has any Level role (Level 1, Level 2, etc.)
            for role in member.roles:
                if role.name.startswith("Level "):
                    return True
            return False
        except Exception as e:
            print(f"Error checking opt-in status for user {user_id}: {e}")
            return False

quest_bot = QuestBot()

@bot.event
async def on_ready():
    print(f'{bot.user} has logged in to Discord!')
    for guild in bot.guilds:
        quest_bot.load_settings(guild.id)
        # Create level roles on startup
        await quest_bot.create_level_roles(guild)
        # Cache members to improve role reading
        try:
            await guild.chunk()
            print(f"Cached {guild.member_count} members for {guild.name}")
        except Exception as e:
            print(f"Failed to cache members for {guild.name}: {e}")
    # Sync slash commands
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} slash commands")
    except Exception as e:
        print(f"Failed to sync slash commands: {e}")

@bot.event
async def on_reaction_add(reaction, user):
    """Handle quest completion reactions and opt-in reactions"""
    if user.bot:
        return
    
    # Check if it's a quest completion (✅ emoji)
    if str(reaction.emoji) == '✅':
        if not quest_bot.db_connection:
            return
        cursor = quest_bot.db_connection.cursor()
        
        # First check if this is an opt-in message (by message ID)
        if quest_bot.optin_message_id and reaction.message.id == quest_bot.optin_message_id:
                # This is an opt-in reaction
                guild = reaction.message.guild
                member = guild.get_member(user.id)
                
                if member:
                    # Check if user already has a level role
                    has_level_role = any(role.name.startswith("Level ") for role in member.roles)
                    
                    if not has_level_role:
                        # Assign Level 1 role
                        level_1_role = discord.utils.get(guild.roles, name="Level 1")
                        if level_1_role:
                            try:
                                await member.add_roles(level_1_role, reason="Opted into QuestBot system")
                                
                                # Initialize user in database
                                cursor.execute('INSERT OR IGNORE INTO users (user_id, guild_id, xp, level) VALUES (?, ?, 0, 1)', 
                                             (user.id, guild.id))
                                quest_bot.db_connection.commit()
                                
                                # Send confirmation message
                                confirmation_embed = discord.Embed(
                                    title="✅ Welcome to QuestBot!",
                                    description=f"{user.mention} has opted into the QuestBot system!\nYou can now earn XP, complete quests, and appear on the leaderboard.",
                                    color=0x00ff00
                                )
                                await reaction.message.channel.send(embed=confirmation_embed, delete_after=10)
                                print(f"User {user.name} opted into QuestBot system")
                            except discord.Forbidden:
                                print(f"Failed to assign Level 1 role to {user.name} - insufficient permissions")
                        else:
                            print("Level 1 role not found - creating level roles")
                            await quest_bot.create_level_roles(guild)
                            level_1_role = discord.utils.get(guild.roles, name="Level 1")
                            if level_1_role:
                                await member.add_roles(level_1_role, reason="Opted into QuestBot system")
                                cursor.execute('INSERT OR IGNORE INTO users (user_id, guild_id, xp, level) VALUES (?, ?, 0, 1)', 
                                             (user.id, guild.id))
                                quest_bot.db_connection.commit()
                return
        
        # Check if it's a quest completion
        cursor.execute('SELECT title, completed_users FROM quests WHERE message_id = ?', (reaction.message.id,))
        quest_data = cursor.fetchone()
        
        if quest_data:
            # Only allow opted-in users to complete quests
            if not quest_bot.is_user_opted_in(user.id, reaction.message.guild.id):
                return
            
            title, completed_users_json = quest_data
            completed_users = json.loads(completed_users_json)
            
            if user.id not in completed_users:
                # Award 50 XP for quest completion
                new_xp, new_level = quest_bot.update_user_xp(user.id, reaction.message.guild.id, 50)
                completed_users.append(user.id)
                
                # Update quest completion list
                cursor.execute('UPDATE quests SET completed_users = ? WHERE message_id = ?', 
                              (json.dumps(completed_users), reaction.message.id))
                if quest_bot.db_connection:
                    quest_bot.db_connection.commit()
                
                # Send confirmation message
                embed = discord.Embed(
                    title="Quest Completed!",
                    description=f"{user.mention} completed: **{title}**\n+50 XP (Total: {new_xp} XP, Level {new_level})",
                    color=0x00ff00
                )
                await reaction.message.channel.send(embed=embed, delete_after=10)

async def check_and_update_level_roles(user_id: int, guild_id: int, reason: str = "XP change"):
    """Comprehensive level role check and update function"""
    try:
        # Get current level in database
        current_data = quest_bot.get_user_data(user_id, guild_id)
        old_level = current_data['level']
        
        # Calculate actual total XP and new level
        current_total_xp = quest_bot.calculate_total_user_xp(user_id, guild_id)
        new_level = quest_bot.calculate_level(current_total_xp)
        
        # Update level in database if changed and trigger level role assignment
        if old_level != new_level:
            cursor = quest_bot.db_connection.cursor()
            cursor.execute('UPDATE users SET level = ? WHERE user_id = ? AND guild_id = ?', 
                          (new_level, user_id, guild_id))
            quest_bot.db_connection.commit()
            asyncio.create_task(quest_bot.update_user_level_role(user_id, guild_id, old_level, new_level))
            return old_level, new_level, current_total_xp
        
        return old_level, old_level, current_total_xp
    except Exception as e:
        print(f"Error in check_and_update_level_roles: {e}")
        return 1, 1, 0

@bot.event
async def on_member_update(before, after):
    """Handle role changes for automatic XP assignment"""
    guild_id = after.guild.id
    
    # Check for role changes (additions OR removals)
    added_roles = set(after.roles) - set(before.roles)
    removed_roles = set(before.roles) - set(after.roles)
    
    # Handle specific role additions (only for opted-in users)
    for role in added_roles:
        # Skip XP assignment for users who haven't opted in
        if not quest_bot.is_user_opted_in(after.id, guild_id):
            continue
            
        role_xp_data = quest_bot.get_role_xp_and_type(guild_id, str(role.id))
        if role_xp_data:
            xp_reward, role_type = role_xp_data
            
            # Handle streak roles differently - accumulate each time they're gained
            if role_type == "streak":
                quest_bot.record_streak_role_gain(after.id, guild_id, role.id, role.name, xp_reward)
                
                # Check for level changes after streak accumulation
                old_level, new_level, total_xp = await check_and_update_level_roles(after.id, guild_id, "streak role gain")
                level_text = f" → Level {new_level}!" if old_level != new_level else ""
                
                # Send notification for streak role gain
                embed = discord.Embed(
                    title="🔥 Streak Role Gained!",
                    description=f"{after.mention} gained **{role.name}** role!\n+{xp_reward} Streak XP accumulated (Total: {total_xp} XP){level_text}",
                    color=0xff6600
                )
            else:
                # Check for level changes after badge role gain
                old_level, new_level, total_xp = await check_and_update_level_roles(after.id, guild_id, "badge role gain")
                level_text = f" → Level {new_level}!" if old_level != new_level else ""
                
                # Send notification for badge role gain
                embed = discord.Embed(
                    title="🏅 Role Gained!",
                    description=f"{after.mention} gained **{role.name}** role!\n+{xp_reward} XP (Total: {total_xp} XP){level_text}",
                    color=0x0099ff
                )
            
            # Try to send to general channel or first available channel
            for channel in after.guild.text_channels:
                if hasattr(channel, 'send') and channel.permissions_for(after.guild.me).send_messages:
                    await channel.send(embed=embed, delete_after=15)
                    break
        elif "badge" in role.name.lower():
            # Handle unassigned badge roles (fallback +5 XP) - only for opted-in users
            old_level, new_level, total_xp = await check_and_update_level_roles(after.id, guild_id, "badge role gain")
            level_text = f" → Level {new_level}!" if old_level != new_level else ""
            
            embed = discord.Embed(
                title="🏅 Role Gained!",
                description=f"{after.mention} gained **{role.name}** role!\n+5 XP (Total: {total_xp} XP){level_text}",
                color=0x0099ff
            )
            
            # Try to send to general channel or first available channel
            for channel in after.guild.text_channels:
                if hasattr(channel, 'send') and channel.permissions_for(after.guild.me).send_messages:
                    await channel.send(embed=embed, delete_after=15)
                    break

@bot.command(name='questbotoptin')
@commands.has_permissions(manage_roles=True)
async def questbot_optin(ctx, channel: discord.TextChannel = None):
    """Create an opt-in embed message for users to join the QuestBot system (admin only)"""
    target_channel = channel or ctx.channel
    
    # Create opt-in embed
    embed = discord.Embed(
        title="🤖 QuestBot Opt-In",
        description="React with ✅ to join the QuestBot system and start earning XP!\n\n"
                   "**What you get by joining:**\n"
                   "• Earn XP by completing quests (50 XP each)\n"
                   "• Gain XP from badge and streak roles\n"
                   "• Appear on the server leaderboard\n"
                   "• Automatic level progression and role assignment\n"
                   "• Track your progress with detailed XP breakdown\n\n"
                   "**Note:** Only opted-in users will earn XP and appear on leaderboards.",
        color=0x0099ff
    )
    embed.add_field(name="📊 Level System", value="Level 1: 0 XP → Level 10: 11,700 XP", inline=False)
    embed.set_footer(text="React with ✅ below to opt in • This is required to use the bot")
    
    try:
        # Send embed to target channel
        optin_message = await target_channel.send(embed=embed)
        await optin_message.add_reaction('✅')
        
        # Store the opt-in message details
        quest_bot.optin_message_id = optin_message.id
        quest_bot.optin_channel_id = target_channel.id
        quest_bot.save_settings(ctx.guild.id)
        
        # Confirm to admin
        admin_embed = discord.Embed(
            title="✅ Opt-In Message Created",
            description=f"QuestBot opt-in message posted in {target_channel.mention}\n"
                       f"Users can now react with ✅ to join the system.",
            color=0x00ff00
        )
        await ctx.send(embed=admin_embed, delete_after=15)
        
    except discord.Forbidden:
        await ctx.send("❌ I don't have permission to send messages or add reactions in that channel!", delete_after=10)
    except Exception as e:
        await ctx.send(f"❌ Error creating opt-in message: {str(e)[:100]}", delete_after=10)

@bot.command(name='assignstreakXP')
@commands.has_permissions(manage_roles=True)
async def assign_streak_xp(ctx, xp_amount: int, *roles: discord.Role):
    """Assign XP value to streak roles - auto-detects if no roles specified"""
    guild_id = ctx.guild.id
    if guild_id not in quest_bot.role_xp_assignments:
        quest_bot.role_xp_assignments[guild_id] = {}
    
    # If roles are provided, use those; otherwise show usage message
    if roles:
        streak_roles = list(roles)
        detection_mode = "specified"  # Fix the missing variable
    else:
        embed = discord.Embed(
            title="❌ No Roles Specified",
            description="Please specify which roles should be streak roles.\n\n**Usage:** `-assignstreakXP 10 @1week @2weeks @1month`\n\n*The system will remember that these roles are streak roles for XP tracking.*",
            color=0xff0000
        )
        await ctx.send(embed=embed)
        return
    
    # List found streak roles and assign XP
    assigned_count = 0
    role_list = ""
    
    for role in streak_roles:
        role_id_str = str(role.id)
        
        # Check if already assigned
        existing_data = quest_bot.get_role_xp_and_type(guild_id, role_id_str)
        if not existing_data:
            quest_bot.assign_role_xp(guild_id, role_id_str, xp_amount, "streak")
            assigned_count += 1
            role_list += f"• **{role.name}** - {xp_amount} Streak XP\n"
        else:
            current_xp, current_type = existing_data
            role_list += f"• **{role.name}** - Already assigned {current_xp} {current_type.title()} XP (skipped)\n"
    
    quest_bot.save_settings(guild_id)
    
    mode_text = "auto-detected" if detection_mode == "auto" else "specified"
    embed = discord.Embed(
        title="🔥 Streak Role XP Assignment",
        description=f"Found {len(streak_roles)} {mode_text} streak role(s). Assigned XP to {assigned_count} new role(s):",
        color=0x00ff00
    )
    
    if role_list:
        embed.add_field(name="Streak Roles", value=role_list[:1024], inline=False)
    
    await ctx.send(embed=embed)

@bot.command(name='leaderboard')
async def leaderboard(ctx):
    """Display the XP leaderboard (opted-in users only)"""
    try:
        leaderboard_data = quest_bot.get_leaderboard(ctx.guild.id, 10)
        
        if not leaderboard_data:
            embed = discord.Embed(
                title="📊 XP Leaderboard",
                description="No opted-in users found yet!\nUse `-questbotoptin` to create an opt-in message, or complete some quests to get on the leaderboard!",
                color=0xffd700
            )
            # Still show level requirements
            level_info = "**Level Requirements:**\n"
            for level, xp in LEVEL_THRESHOLDS.items():
                level_info += f"Level {level}: {xp:,} XP\n"
            embed.add_field(name="Level System", value=level_info, inline=False)
            await ctx.send(embed=embed)
            return
        
        embed = discord.Embed(
            title="🏆 XP Leaderboard",
            description="Top 10 Opted-In Quest Completers",
            color=0xffd700
        )
        
        medals = ["🥇", "🥈", "🥉"]
        users_added = 0
        
        for i, (user_id, xp, level) in enumerate(leaderboard_data):
            medal = medals[i] if i < 3 else f"#{i+1}"
            
            # Try multiple methods to get user info
            user = ctx.guild.get_member(user_id)
            if not user:
                user = bot.get_user(user_id)
            
            # Calculate total XP including role-based XP
            total_xp = quest_bot.calculate_total_user_xp(user_id, ctx.guild.id)
            
            if user:
                # Format username without pinging - use @ but escape it
                username = f"@{user.name}"
                display_name = getattr(user, 'display_name', user.name)
                if display_name != user.name:
                    username = f"@{user.name} ({display_name})"
                
                embed.add_field(
                    name=f"{medal} Level {level}",
                    value=f"{username}\n{total_xp:,} XP",
                    inline=True
                )
                users_added += 1
            else:
                # Last resort - show user ID
                embed.add_field(
                    name=f"{medal} Level {level}",
                    value=f"@User{str(user_id)[-4:]}\n{total_xp:,} XP",
                    inline=True
                )
                users_added += 1
        
        if users_added == 0:
            embed.add_field(
                name="No Active Users", 
                value="Opted-in users with XP may have left the server", 
                inline=False
            )
        
        # Add level requirements info
        level_info = "**Level Requirements:**\n"
        for level, xp_req in LEVEL_THRESHOLDS.items():
            level_info += f"Level {level}: {xp_req:,} XP\n"
        
        embed.add_field(name="Level System", value=level_info, inline=False)
        embed.set_footer(text="Only opted-in users appear on this leaderboard")
        await ctx.send(embed=embed)
        
    except Exception as e:
        print(f"Error in leaderboard command: {e}")
        await ctx.send("❌ Could not retrieve leaderboard data. Please try again later.", delete_after=5)

@bot.command(name='checkXP')
async def check_xp(ctx, member: discord.Member = None):
    """Check your current XP and level progress (opted-in users only)"""
    try:
        # If no member specified, check the command user's XP
        target_member = member or ctx.author
        
        # Check if target user is opted in
        if not quest_bot.is_user_opted_in(target_member.id, ctx.guild.id):
            if target_member == ctx.author:
                embed = discord.Embed(
                    title="❌ Not Opted In",
                    description="You haven't opted into the QuestBot system yet!\n\n"
                               "Ask an admin to use `-questbotoptin` to create an opt-in message, "
                               "then react with ✅ to join the system and start earning XP!",
                    color=0xff0000
                )
            else:
                embed = discord.Embed(
                    title="❌ User Not Opted In",
                    description=f"{target_member.mention} hasn't opted into the QuestBot system yet.\n\n"
                               "Only users who have opted in can have their XP checked.",
                    color=0xff0000
                )
            await ctx.send(embed=embed, delete_after=15)
            return
        
        # Get XP breakdown for detailed display
        guild = ctx.guild
        guild_id = guild.id
        
        # Use the same XP calculation method as leaderboard for consistency
        current_xp = quest_bot.calculate_total_user_xp(target_member.id, guild_id)
        current_level = quest_bot.calculate_level(current_xp)
        
        # Get base XP for breakdown
        user_data = quest_bot.get_user_data(target_member.id, guild_id)
        base_xp = user_data.get('xp', 0)
        
        # Calculate XP needed for next level
        next_level = min(current_level + 1, 10)  # Cap at level 10
        next_level_xp = LEVEL_THRESHOLDS.get(next_level, LEVEL_THRESHOLDS[10])
        xp_needed = max(0, next_level_xp - current_xp)
        
        # Calculate progress percentage safely
        if current_level < 10:
            current_level_xp = LEVEL_THRESHOLDS.get(current_level, 0)
            xp_range = next_level_xp - current_level_xp
            if xp_range > 0:
                progress_percentage = min(100, max(0, ((current_xp - current_level_xp) / xp_range) * 100))
            else:
                progress_percentage = 100
        else:
            progress_percentage = 100
        
        embed = discord.Embed(
            title=f"📊 {target_member.display_name}'s XP Stats",
            color=0x00ff00
        )
        
        embed.add_field(name="💰 Total XP", value=f"{current_xp:,} XP", inline=True)
        embed.add_field(name="⭐ Current Level", value=f"Level {current_level}", inline=True)
        embed.add_field(name="🎯 Quest XP", value=f"{base_xp:,} XP", inline=True)
        
        if current_level < 10:
            embed.add_field(name="🎯 XP to Next Level", value=f"{xp_needed:,} XP needed", inline=True)
            
            # Progress bar with safe calculation
            progress_bar_length = 20
            filled_length = int(progress_bar_length * progress_percentage / 100)
            filled_length = max(0, min(filled_length, progress_bar_length))  # Clamp values
            bar = "█" * filled_length + "░" * (progress_bar_length - filled_length)
            embed.add_field(
                name="📈 Progress to Next Level", 
                value=f"`{bar}` {progress_percentage:.1f}%", 
                inline=False
            )
        else:
            embed.add_field(name="🏆 Status", value="**MAX LEVEL REACHED!**", inline=True)
        
        # Safe avatar handling
        try:
            if target_member.avatar:
                embed.set_thumbnail(url=target_member.avatar.url)
            else:
                embed.set_thumbnail(url=target_member.default_avatar.url)
        except:
            pass  # Skip thumbnail if there are issues
        
        embed.set_footer(text="Complete quests and gain roles to earn XP! • Opted-in users only")
        
        await ctx.send(embed=embed)
        
    except Exception as e:
        import traceback
        print(f"Error in checkXP command: {e}")
        print(f"Traceback: {traceback.format_exc()}")
        await ctx.send(f"❌ Could not retrieve XP data. Error: {str(e)[:100]}...", delete_after=10)

@bot.command(name='addXP')
@commands.has_permissions(manage_roles=True)
async def add_xp_command(ctx, member: discord.Member, amount: int):
    """Add XP to a user (admin only, opted-in users only)"""
    # Check if target user is opted in
    if not quest_bot.is_user_opted_in(member.id, ctx.guild.id):
        embed = discord.Embed(
            title="❌ User Not Opted In",
            description=f"{member.mention} hasn't opted into the QuestBot system yet.\n\n"
                       "Only opted-in users can receive XP. Ask them to react to the opt-in message first.",
            color=0xff0000
        )
        await ctx.send(embed=embed, delete_after=10)
        return
    
    try:
        # Update user XP
        new_total_xp, new_level = quest_bot.update_user_xp(member.id, ctx.guild.id, amount)
        
        # Send confirmation
        embed = discord.Embed(
            title="✅ XP Added",
            description=f"Added {amount:,} XP to {member.mention}\n"
                       f"**New Total:** {new_total_xp:,} XP (Level {new_level})",
            color=0x00ff00
        )
        await ctx.send(embed=embed, delete_after=10)
        
    except Exception as e:
        await ctx.send(f"❌ Error adding XP: {str(e)[:100]}", delete_after=10)

@bot.command(name='removeXP')
@commands.has_permissions(manage_roles=True)
async def remove_xp_command(ctx, member: discord.Member, amount: int):
    """Remove XP from a user (admin only, opted-in users only)"""
    # Check if target user is opted in
    if not quest_bot.is_user_opted_in(member.id, ctx.guild.id):
        embed = discord.Embed(
            title="❌ User Not Opted In",
            description=f"{member.mention} hasn't opted into the QuestBot system yet.\n\n"
                       "Only opted-in users can have XP modified.",
            color=0xff0000
        )
        await ctx.send(embed=embed, delete_after=10)
        return
    
    try:
        # Remove user XP (negative amount)
        new_total_xp, new_level = quest_bot.update_user_xp(member.id, ctx.guild.id, -amount)
        
        # Send confirmation
        embed = discord.Embed(
            title="✅ XP Removed",
            description=f"Removed {amount:,} XP from {member.mention}\n"
                       f"**New Total:** {new_total_xp:,} XP (Level {new_level})",
            color=0x00ff00
        )
        await ctx.send(embed=embed, delete_after=10)
        
    except Exception as e:
        await ctx.send(f"❌ Error removing XP: {str(e)[:100]}", delete_after=10)

@bot.command(name='setXP')
@commands.has_permissions(manage_roles=True)
async def set_xp_command(ctx, member: discord.Member, amount: int):
    """Set a user's XP to a specific amount (admin only, opted-in users only)"""
    # Check if target user is opted in
    if not quest_bot.is_user_opted_in(member.id, ctx.guild.id):
        embed = discord.Embed(
            title="❌ User Not Opted In",
            description=f"{member.mention} hasn't opted into the QuestBot system yet.\n\n"
                       "Only opted-in users can have XP modified.",
            color=0xff0000
        )
        await ctx.send(embed=embed, delete_after=10)
        return
    
    try:
        # Get current XP to calculate the difference
        current_data = quest_bot.get_user_data(member.id, ctx.guild.id)
        current_base_xp = current_data.get('xp', 0)
        xp_change = amount - current_base_xp
        
        # Update user XP to the target amount
        new_total_xp, new_level = quest_bot.update_user_xp(member.id, ctx.guild.id, xp_change)
        
        # Send confirmation
        embed = discord.Embed(
            title="✅ XP Set",
            description=f"Set {member.mention}'s base XP to {amount:,} XP\n"
                       f"**Total XP:** {new_total_xp:,} XP (Level {new_level})",
            color=0x00ff00
        )
        await ctx.send(embed=embed, delete_after=10)
        
    except Exception as e:
        await ctx.send(f"❌ Error setting XP: {str(e)[:100]}", delete_after=10)

@bot.command(name='questbot')
async def questbot_ping(ctx):
    """Ping the bot to check if it's online"""
    await ctx.send("online")

# Error handling
@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ You don't have permission to use this command!", delete_after=5)
    elif isinstance(error, commands.MissingRole):
        await ctx.send("❌ You need the @staff role to use this command!", delete_after=5)
    elif isinstance(error, commands.BadArgument):
        await ctx.send("❌ Invalid argument provided!", delete_after=5)
    else:
        await ctx.send("❌ An error occurred while processing the command!", delete_after=5)

if __name__ == "__main__":
    import os
    
    # Get token from environment variable
    TOKEN = os.getenv('DISCORD_BOT_TOKEN')
    
    if not TOKEN:
        print("Error: DISCORD_BOT_TOKEN environment variable not set!")
        print("Please set your Discord bot token as an environment variable.")
        exit(1)
    
    # Run the bot
    bot.run(TOKEN)
