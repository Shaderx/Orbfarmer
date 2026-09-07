"""
ui.py – Terminal UI helpers: colors, banners, animations, menus, credits.
"""

import sys
import time

from . import config


class Colors:
    RESET   = '\033[0m'
    BOLD    = '\033[1m'
    RED     = '\033[91m'
    GREEN   = '\033[92m'
    YELLOW  = '\033[93m'
    BLUE    = '\033[94m'
    MAGENTA = '\033[95m'
    CYAN    = '\033[96m'
    WHITE   = '\033[97m'
    GRAY    = '\033[90m'


def print_color(text: str, color: str = Colors.WHITE, bold: bool = False) -> None:
    """Print colored text."""
    style = Colors.BOLD if bold else ''
    print(f"{style}{color}{text}{Colors.RESET}")


def print_boxed_title(title: str, width: int = 50, color: str = Colors.CYAN) -> None:
    """Print a boxed title with ASCII borders."""
    border = f"{Colors.BOLD}{color}{'+' + '-' * (width - 2) + '+'}{Colors.RESET}"
    title_padding = (width - len(title) - 4) // 2
    extra_space = (width - len(title) - 4) % 2
    title_line = (
        f"{Colors.BOLD}{color}|{Colors.RESET}"
        f"{' ' * title_padding}{Colors.BOLD}{title}{Colors.RESET}"
        f"{' ' * (title_padding + extra_space)}"
        f"{Colors.BOLD}{color}|{Colors.RESET}"
    )
    print(f"\n{border}")
    print(title_line)
    print(f"{border}\n")


def print_banner() -> None:
    """Display ASCII banner."""
    banner = f"""
{Colors.CYAN}{Colors.BOLD}
                                                                
    O R B F A R M E R
                                                                
{Colors.RESET}
    {Colors.GRAY}Developer: {Colors.CYAN}{config.DEVELOPER}{Colors.RESET}
    {Colors.GRAY}Version: {Colors.WHITE}{config.VERSION}{Colors.RESET}
    {Colors.GRAY}Database: {Colors.GREEN}Discord Official API + GitHub Archive{Colors.RESET}
"""
    print(banner)


def loading_animation(text: str, duration: float = 1.5) -> None:
    """Display loading animation."""
    frames = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
    ascii_frames = ["|", "/", "-", "\\"]
    end_time = time.time() + duration
    i = 0
    while time.time() < end_time:
        try:
            sys.stdout.write(f"\r{Colors.CYAN}{frames[i % len(frames)]}{Colors.RESET} {text}")
            sys.stdout.flush()
        except UnicodeEncodeError:
            try:
                sys.stdout.write(f"\r{Colors.CYAN}{ascii_frames[i % len(ascii_frames)]}{Colors.RESET} {text}")
                sys.stdout.flush()
            except Exception:
                pass
        except Exception:
            pass
        time.sleep(0.1)
        i += 1
    try:
        sys.stdout.write("\r" + " " * (len(text) + 5) + "\r")
        sys.stdout.flush()
    except Exception:
        pass


def ask_confirm(prompt: str = "Create and launch?") -> bool:
    """Ask a Y/n confirmation question. Returns True if user confirms."""
    answer = input(f"\n{Colors.BOLD}{prompt}{Colors.RESET} [Y/n]: ").strip().lower()
    return answer in ('', 'y', 'yes')


def print_menu() -> None:
    """Display main menu."""
    print_boxed_title("MAIN MENU", width=50, color=Colors.CYAN)
    print(f"  {Colors.BOLD}{Colors.GREEN}1.{Colors.RESET} Search Discord database (Official API)")
    print(f"  {Colors.BOLD}{Colors.GREEN}2.{Colors.RESET} Manual mode (custom executable)")
    print(f"  {Colors.BOLD}{Colors.YELLOW}3.{Colors.RESET} Steam Quest Mode (Steam catalog search)")
    print(f"  {Colors.BOLD}{Colors.GREEN}4.{Colors.RESET} Credits & Info")
    print(f"  {Colors.BOLD}{Colors.RED}5.{Colors.RESET} Exit\n")


def show_credits() -> None:
    """Display credits."""
    print_boxed_title("CREDITS", width=65, color=Colors.CYAN)
    credits_text = f"""
    {Colors.BOLD}Developer:{Colors.RESET} {Colors.CYAN}{config.DEVELOPER}{Colors.RESET}
    {Colors.BOLD}Version:{Colors.RESET}   {Colors.WHITE}{config.VERSION}{Colors.RESET}

    {Colors.BOLD}Description:{Colors.RESET}
    This tool works as a game process spoofer. It tricks Discord into
    thinking you're running a game by creating fake processes with the
    exact names Discord expects.
    
    {Colors.BOLD}IMPORTANT:{Colors.RESET} {Colors.RED}Discord MUST be running for this to work!{Colors.RESET}
    
    {Colors.BOLD}How it works (Game Spoofing):{Colors.RESET}
    1. Connects to Discord's official API to get the latest game list
    2. Finds the exact process name Discord expects for each game
    3. Creates the matching executable path under simulations/
    4. Opens a visible game-themed timer window
    5. Keep the window open and check detection and quest progress in Discord
    
    {Colors.BOLD}Steam Quest Mode:{Colors.RESET}
    1. Searches Steam for game metadata, hero artwork, and an icon
    2. Creates the game-specific path in the local simulations folder
    3. Press Enter in the main app to stop and clean up the session
    Steam libraries and existing files are preserved.
    
    {Colors.BOLD}Database Sources:{Colors.RESET}
    • Primary: Discord Official API
    • Backup:  GitHub Archive by Cynosphere
    
    {Colors.BOLD}Pro Tips:{Colors.RESET}
    • Use Steam Quest Mode to search Steam's catalog
    • Find AppIDs at https://steamdb.info
    • The fake process must stay running for Discord to detect it
    
    {Colors.BOLD}{Colors.GREEN}Multi-Game Emulation:{Colors.RESET}
    • Run this tool multiple times to emulate multiple games at once
    • Check each quest's requirements and progress in Discord
    
    {Colors.BOLD}{Colors.RED}WARNING - EDUCATIONAL PURPOSES ONLY{Colors.RESET}
    • Users are SOLELY responsible for compliance with Discord ToS
    • The developers are NOT responsible for any consequences
    • Use at your own risk
    
    {Colors.GRAY}Made by {config.DEVELOPER}{Colors.RESET}
    {Colors.GRAY}Original project by {config.ORIGINAL_DEVELOPERS}{Colors.RESET}
    {Colors.CYAN}Contributions by {config.CONTRIBUTOR}: detection fixes, cleanup improvements, and themed timer UI.{Colors.RESET}
    {Colors.GRAY}Press Enter to return to menu...{Colors.RESET}
"""
    print(credits_text)
    input()
