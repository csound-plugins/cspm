#!/usr/bin/env python3

import os
import argparse
import dataclasses
import json
import sys
import tempfile
import shutil
import subprocess
import textwrap
import fnmatch
from urllib.parse import urlparse
from pathlib import Path
from typing import List, Dict, Tuple, Union, Optional

VERSION = "0.1.4"

GIT_REPOSITORY = "https://github.com/csound-plugins/risset-data"

settings = {
    'debug': False
}

def debug(*msgs: str) -> None:
    if settings['debug']:
        print("DEBUG: ", *msgs, file=sys.stderr)


def errormsg(s: str) -> None:
    for line in s.splitlines():
        print("** Error: ", line, file=sys.stderr)


def banner(lines: List[str]):
    margin = 2
    marginstr = " " * margin
    sep = "*" * (margin*2 + max(len(line) for line in lines))
    print("", sep, sep, "", sep="\n", end="")
    for line in lines:
        print(marginstr, line)
    print("", sep, sep, "", sep="\n", end="")
    

class ErrorMsg(str):
    pass


@dataclasses.dataclass
class Binary:
    platform: str
    url: str
    build_platform: str


@dataclasses.dataclass
class Plugin:
    name: str
    libname: str
    version: str
    short_description: str
    csound_version: str
    binaries: Dict[str, Binary]
    opcodes: List[str]
    author: str
    email: str
    long_description: str = ""
    manual: str = ""
    source: str = ""   # The path/url of the manifest


@dataclasses.dataclass
class InstalledPluginInfo:
    name: str
    path: Path
    installed_in_system_folder: bool
    versionstr: Optional[str] 
    installed_manifest_path: Optional[Path] = None 
    

UNKNOWN_VERSION = "Unknown"


class PlatformNotSupportedError(Exception): pass


def _plugin_extension() -> str:
    ext = {
        'linux': '.so',
        'darwin': '.dylib',
        'win32': '.dll'
    }.get(sys.platform)
    if not ext:
        raise PlatformNotSupportedError(f"Platform {sys.platform} is not supported")
    return ext


def _get_path_separator() -> str:
    """Returns the path separator for the current platform"""
    if sys.platform == "win32":
        return ";"
    return ":"


def _get_platform() -> str:
    """Returns one of "linux", "macos", "windows" """
    platform = {
        'linux': 'linux',
        'darwin': 'macos',
        'win32': 'windows'
    }.get(sys.platform)
    if not platform:
        raise SystemError("Platform {sys.platform} not supported")
    return platform


def _get_shell() -> Optional[str]:
    """ Returns one of "bash", "zsh", "fish" 
    If not able to get the given information, returns None
    In particular, in windows it returns None
    """
    if sys.platform == "win32":
        return
    shellenv = os.getenv("SHELL")
    if not shellenv:
        return None
    shell = os.path.split(shellenv)[1].strip()
    if shell in ("bash", "zsh", "fish"):
        return shell
    return None


def _git_clone(repo:str, destination:Path) -> None:
    """
    Clone the given repository to the destination.
    """
    if not isinstance(destination, Path):
        raise TypeError("destination should be a Path")
    if not destination.is_absolute():
        raise ValueError("Destination should be an absolute path")
    if destination.exists():
        raise OSError("Destination path already exists, can't clone git repository")
    gitbin = shutil.which("git")
    if not gitbin:
        raise OSError("Could not find git binary. Is it in the path?")
    parent = destination.parent
    if not parent.exists():
        parent.mkdir(parents=True, exist_ok=True)
    subprocess.call([gitbin, "clone", repo, str(destination)])
    # check the repository
    indexfile = destination / "plugins.json"
    if not indexfile.exists():
        raise RuntimeError("Git repository was not cloned properly,"
                           " can't find plugins.json file")


def _git_update(repopath:Path):
    """
    Update the git repo at the given path
    """
    debug(f"Updating git repository: {repopath}")
    if not repopath.exists():
        raise OSError(f"Can't find path to git repository {repopath}")
    gitbin = shutil.which("git")
    if not gitbin:
        raise OSError("Could not find git binary. Is it in the path?")
    cwd = os.path.abspath(os.path.curdir)
    os.chdir(str(repopath))
    if settings['debug']:
        subprocess.call([gitbin, "pull"])
    else:
        subprocess.call([gitbin, "pull"], stdout=subprocess.PIPE)
    os.chdir(cwd)


def _copy_with_sudo(src: str, dst: str) -> Optional[ErrorMsg]:
    debug(f"(sudo) Copying {src} -> {dst} ")
    try:
        subprocess.call(["sudo", "cp", src, dst])
    except KeyboardInterrupt:
        return ErrorMsg("User cancelled")
    except Exception as e:
        return ErrorMsg(e)
    return None


def _sudo_rm(path: str) -> Optional[ErrorMsg]:
    """
    Remove a file with sudo

    Args:
        path: the path of the file to remove
    """
    debug(f"(sudo) rm {path}")
    print(f"\n  Administrator rights are needed to remove {path}\n")

    try:
        subprocess.call(["sudo", "rm", "-i", path])
    except KeyboardInterrupt:
        return ErrorMsg("User cancelled")
    except Exception as e:
        return ErrorMsg(str(e))
    return None


def version_tuplet(versionstr:str) -> Tuple[int, int, int]:
    if not versionstr:
        raise ValueError("versionstr is empty")
    parts = versionstr.split(".")
    try:
        ints = [int(part) for part in parts]
    except ValueError:
        raise ValueError(f"Could not parse version {versionstr}")
        
    if len(ints) == 1:
        ints += [0, 0]
    elif len(ints) == 2:
        ints.append(0)
    elif len(ints) > 3:
        debug("Too many version parts (max. 3), using the first 3")
        ints = ints[:3]
    i1, i2, i3 = ints
    return i1, i2, i3
    

def _find_opcodes_dir(possible_dirs) -> Optional[Path]:
    """
    Given a list of possible paths, find the folder where
    the system plugins are installed
    """
    ext = _plugin_extension() 
    debug("Finding opcodes dir: ")

    if sys.platform == "win32":
        portaudio_dll = "rtpa.dll"
    else:
        portaudio_dll = "librtpa" + ext

    for d in possible_dirs:
        debug("   looking at ", d)
        d = _normalize_path(d)
        path = Path(d)
        if not path.is_dir() or not path.exists():
            continue
        plugins = list(path.glob("*" + ext))
        if not plugins:
            debug(f"Path {d} exists, but has no plugins, skipping")
            continue
        if any(plugin for plugin in plugins if portaudio_dll == plugin.name):
            return path
        errormsg(f"Found plugins dir {d}, but it does not seem to be the systems"
                 f" plugin path ({portaudio_dll} should be present but was not found)")
    return None


def _data_dir_for_platform():
    """
    Returns the data directory for the given platform
    """
    platform = sys.platform
    if platform == 'linux':
        return Path(os.path.expanduser("~/.local/share"))
    elif platform == 'darwin':
        return Path(os.path.expanduser("~/Libary/Application Support"))
    elif platform == 'win32':
        p = R"C:\Users\$USERNAME\AppData\Local"
        return Path(os.path.expandvars(p))
    else:
        raise ValueError(f"Platform unknown: {platform}")


def _load_manifest(path: str) -> Union[dict, ErrorMsg]:
    assert os.path.splitext(path)[1] == ".json"
    try:
        d = json.load(open(path))
        return d
    except Exception as e:
        errormsg(f"Could not parse manifest {path}")
        return ErrorMsg(str(e))


def is_url(value:str) -> bool:
    """
    Return whether or not given value is a valid URL.
    Args:
        value: URL address string to validate
    """
    result = urlparse(str(value))
    return bool(result.scheme and result.netloc)


def _parse_pluginkey(pluginkey: str) -> Tuple[str, str]:
    """
    Given a key pluginname@version, return (pluginname, version)
    Handle cases where the pluginkey has no version
    """
    if "@" in pluginkey:
        name, version = pluginkey.split("@")
    else:
        name = pluginkey
        version = "0.0.0"
    return name, version


class PluginDefinitionError(Exception):
    pass


def _normalize_version(version: str, default="0.0.0") -> str:
    try:
        versiontup = version_tuplet(version)
    except ValueError as e:
        debug(f"Error while parsing version {version}: %s", str(e))
        return default
    return ".".join(str(i) for i in versiontup)
    

def _parse_binary(platform:str, binary_definition:dict) -> Union[Binary, ErrorMsg]:
    url = binary_definition.get('url')
    if not url:
        return ErrorMsg(f"Plugin definition for {platform} should have an url")
    build_platform = binary_definition.get('build_platform')
    if not build_platform:
        return ErrorMsg(f"Plugin definition for {platform} should have a build_platform")
    return Binary(platform=platform, url=url, build_platform=build_platform)


def _plugin_from_dict(d: dict) -> Plugin:
    def get_key(key):
        value = d.get(key)
        if value is None:
            raise PluginDefinitionError(f"Plugin has no {key} key")
        return value

    version = _normalize_version(get_key('version'))
    binariesd = get_key('binaries')
    results = [_parse_binary(platform, binary_definition)
               for platform, binary_definition in binariesd.items()]

    binaries = {}
    for result in results:
        if isinstance(result, ErrorMsg):
            errormsg(result)
        else:
            binaries[result.platform] = result
    
    if not binaries:
        raise PluginDefinitionError("No valid binaries defined")
    return Plugin(
        name=get_key('name'),
        libname=get_key('libname'),
        version=version,
        short_description=get_key('short_description'),
        author=get_key('author'),
        email=get_key('email'),
        csound_version=get_key('csound_version'),
        opcodes=get_key('opcodes'),
        binaries=binaries,
        manual=d.get('manual', ''),
        long_description=d.get('long_description', ''),
    )

def resolve_path(filepath: str, cwd:str=None) -> Path:
    """
    If filepath is relative, use cwd as base to convert it
    to an absolute path. If cwd is not given, use the current 
    working dir
    """
    p = Path(filepath)
    if p.is_absolute():
        return p.resolve()
    if cwd is None:
        return (Path.cwd()/p).resolve()
    return (Path(cwd)/p).resolve()


def plugin_definition_from_file(filepath: str, 
                                indexfolder:str=""
                                ) -> Union[Plugin, ErrorMsg]:
    """
    Args:
        filepath: if relative, it is relative to the index file
        indexfolder: the path where the plugins.json file is (a folder).

    Returns:
        either a Plugin, or an ErrorMsg

    Raises PluginDefinitionError if the .json definitionis invalid
    """
    if not filepath.startswith("/"):
        if not indexfolder:
            # filepath is relative and no indexfolder, this is an error
            return ErrorMsg("filepath can't be relative becaus no index folder was given")
        if not os.path.isdir(indexfolder):
            return ErrorMsg("index folder should be a foler")
        if not os.path.exists(indexfolder):
            return ErrorMsg("index folder does not exist")
        path = resolve_path(filepath, indexfolder)
    else:
        # absolute path
        path = Path(filepath).resolve()

    if not path.exists():
        return ErrorMsg(f"plugin definition file ({path}) not found")

    assert path.suffix == ".json", "Plugin definition file should be a .json file"

    debug(f"Parsing {path}")
    d = json.load(open(path))
    try:
        plugin = _plugin_from_dict(d)
        plugin.source = str(path)
    except PluginDefinitionError as e:
        raise e

    return plugin


def _load_url(url:str) -> str:
    import urllib.request
    debug(f"Parsing url: {url}")
    with urllib.request.urlopen(url) as response:
        with tempfile.NamedTemporaryFile(delete=False) as tmp_file:
            shutil.copyfileobj(response, tmp_file)
            text = open(tmp_file.name).read()
            return text


def load_text(file_or_url:str) -> str:
    if is_url(file_or_url):
        return _load_url(file_or_url)
    assert os.path.exists(file_or_url)
    text = open(file_or_url).read()
    debug(f"load_text: {text}")
    if not text:
        debug("load_text: empty file")
    return text


def _normalize_path(path:str) -> str:
    path = os.path.expandvars(path)
    path = os.path.expanduser(path)
    path = os.path.abspath(path)
    return path

class PluginsIndex:

    def __init__(self, version:str, plugins:List[Plugin]):
        self.version = version
        self.plugins = plugins
        self.platform = _get_platform()  # linux, macos, windows
        self.csoundlib = "CsoundLib64"
        self.apiversion = "6.0"
        self.system_plugins_path = self.get_system_plugins_path()
        debug(f"System plugins path: {self.system_plugins_path}")
        self.user_plugins_path = self.get_user_plugins_path()
        if self.system_plugins_path is None:
            errormsg("Could not find the system plugin folder")

    def is_user_plugins_path_set(self) -> bool:
        """
        Returns True if the user has already set the user_plugins_path 
        as part of OPCODE6DIR64. 
        """
        opcode6dir64 = os.getenv("OPCODE6DIR64")
        if not opcode6dir64:
            return False
        sep = _get_path_separator()
        userpaths = [_normalize_path(path) for path in opcode6dir64.split(sep)]
        if str(self.user_plugins_path) in userpaths:
            return True
        if settings['debug']:
            debug("User plugins path not set")
            debug("    user plugins path for this platform: ", str(self.user_plugins_path))
            debug("    OPCODE6DIR64 is set to ", *userpaths)
        return False

    # def set_user_plugins_path(self) -> Optional[ErrorMsg]:
    #     opcode6dir64 = os.getenv("OPCODE6DIR64")
    #     if opcode6dir64:
    #         return ErrorMsg(
    #             "set_user_plugins_path: This operation can only be done if"
    #             f" OPCODE6DIR64 is not set. This variable is already set to {opcode6dir64}")
    #     if self.platform == "linux":
    #         _add_line_check("~/.pam_environment", "OPCODE6DIR64", 
    #                         f"OPCODE6DIR64 DEFAULT={systempath}:{userpath}")
    #     elif self.platform == "macos":
    #         _add_line_check("~/.bash_profile", "OPCODE6DIR64",
    #                         f"export OPCODE6DIR64=\"{systempath}:{userpath}\"")
    #     else:
    #         return ErrorMsg("This platform does not support setting the user plugins path")

    def _user_plugins_path_message(self) -> List[str]:
        """
        Creates a message advising how to modify the environment to 
        add a user plugin path
        """
        sep = _get_path_separator()
        lines: List[str] = []
        userpath = self.user_plugins_path
        systempath = self.system_plugins_path
        _ = lines.append
        _("The path for user plugins is not set.")
        _("To set it you need to modify the environment variable OPCODE6DIR64 to be")
        _(f"OPCODE6DIR64=\"{self.system_plugins_path}{sep}{self.user_plugins_path}\"\n")
        if self.platform == "linux":
            _("NB: If you set the environment variable in a place like ~/.bash_profile")
            _("  it will not be visible by GUI applications.")
            _("  A solution is to add the following line to ~/.pam_environment")
            _("  (create it if it does not exist):\n")
            _(f"  OPCODE6DIR64 DEFAULT={systempath}:/home/@{{PAM_USER}}/.local/share/csound6/plugins64")
        elif self.platform == "macos":
            shell = _get_shell()
            # only print this message for users who have not modified their shell,
            # assuming that someone who modifies it knows in general what they are 
            # doing
            if shell == "bash": 
                _("To set a user path for plugins add the following line to ~/.bash_profile:\n")
                _(f"    export OPCODE6DIR64=\"{systempath}:{userpath}\"\n")
        return lines

    def get_system_plugins_path(self) -> Optional[Path]:
        if self.platform == 'linux':
            possible_dirs = ["/usr/local/lib/csound/plugins64-6.0"]
        elif self.platform == 'macos':
            # The path based on ~ is used when csound is compiled from source.
            # We give that priority since if a user is doing that, it is probably someone who knows
            # what she is doing
            possible_dirs = [
                f"~/Library/Frameworks/{self.csoundlib}.framework/Versions/{self.apiversion}/Resources/Opcodes64",
                f"/Library/Frameworks/{self.csoundlib}.framework/Versions/{self.apiversion}/Resources/Opcodes64",
            ]
        elif self.platform == "windows":
            possible_dirs = ["C:\\Program Files\\Csound6_x64\\plugins64"]
        else:
            return None

        out = _find_opcodes_dir(possible_dirs)
        if not out:
            errormsg(f"System plugins path not found! Searched paths: {possible_dirs}")
            return None
        assert isinstance(out, Path)
        assert out.exists()
        assert out.is_dir()
        assert out.is_absolute()
        return out

    def get_user_plugins_path(self) -> Optional[Path]:
        """ Return the install path for user plugins. Does not check if it is properly set """
        data_dir = _data_dir_for_platform()
        return data_dir / "csound6/plugins64"

    def get_user_installed_dlls(self) -> List[Path]:
        """
        Return a list of plugins installed at the user plugin path. If the user path is not set,
        returns an empty list
        """
        if not self.is_user_plugins_path_set() or self.user_plugins_path is None:
            debug("get installed dlls: user plugins path not set")
            return []
        ext = _plugin_extension()
        return list(self.user_plugins_path.glob("*"+ext))

    def get_system_installed_dlls(self) -> List[Path]:
        if not self.system_plugins_path:
            debug("System plugins path not set!")
            return []
        ext = _plugin_extension()
        return list(self.system_plugins_path.glob("*" + ext))
        
    def get_installed_dlls(self) -> List[Path]:
        """
        Returns a list of all dlls installed in this system
        """
        return self.get_system_installed_dlls() + self.get_user_installed_dlls()

    def is_dll_installed(self, libname:str) -> bool:
        """
        Returns True if libname is installed

        Args:
            libname: the name of the plugin library (without extension)
        """
        dll, user_installed = self.get_installed_path_for_libname(libname)
        if dll:
            return True
        if settings['debug']:
            debug(f">>>> {dll} not installed. Installed dlls:")
            installed_dlls = self.get_installed_dlls()
            for dll in sorted(installed_dlls):
                debug("    ", dll.name, str(dll))
        return False
        
    def get_installed_path_for_libname(self, libname:str) -> Tuple[Optional[Path], bool]:
        """
        Returns (path to dll, user_installed)
        If not installed returns (None, False)

        A user installed dll has priority over system installed

        Args:
            libname: the name of the plugin library, without extension (libfoo)

        Returns:
            A tuple (path to the actual file or None if not found, True if this is inside the user plugins path)
        """
        dll = libname + _plugin_extension()
        user_dlls = self.get_user_installed_dlls()
        for user_dll in user_dlls:
            if user_dll.name == dll:
                return user_dll, True
        system_dlls = self.get_system_installed_dlls()
        for system_dll in system_dlls:
            if system_dll.name == dll:
                return system_dll, False
        return None, False

    def find_plugin(self, plugin_name:str) -> Optional[Plugin]:
        """
        Given a plugin name, find the Plugin definition

        Args:
            plugin_name: the name of the plugin as found in the name field in the manifest
        """
        for plugin in self.plugins:
            if plugin.name == plugin_name:
                return plugin
        return None

    def get_installed_plugin_info(self, plugin_name:str) -> Optional[InstalledPluginInfo]:
        """
        Returns an InstalledPluginInfo if found, None otherwise
        """
        plugin = self.find_plugin(plugin_name)
        if not plugin:
            raise KeyError(f"Plugin {plugin_name} unknown")

        debug(f"Checking if plugin {plugin_name} is installed")
        dll, user_installed = self.get_installed_path_for_libname(plugin.libname)
        if not dll:
            # plugin is not installed
            debug(f"plugin {plugin_name} is not installed yet")
            return None

        installed_version = UNKNOWN_VERSION
        installed_manifest_path = None

        for manifest in self.get_installed_manifests():
            pluginkey = manifest.name.split(".")[0]
            name, version = _parse_pluginkey(pluginkey)
            if name == plugin_name:
                result = _load_manifest(manifest.as_posix())
                if isinstance(result, ErrorMsg):
                    errormsg(str(result))
                    continue
                installed_version = result['version']
                installed_manifest_path = manifest
                break
        
        out = InstalledPluginInfo(
            name = plugin.name,
            path = dll,
            versionstr = installed_version,
            installed_in_system_folder = str(dll.parent) == str(self.system_plugins_path),
            installed_manifest_path = installed_manifest_path
        )
        return out

    def get_plugin_installed_version(self, plugin_name: str) -> Optional[str]:
        """
        Check if the dll is installed, return its version

        Returns None if the dll is not installed, UNKNOWN_VERSION if the
        dll is installed but there is no corresponding installation
        manifest (it was not installed via risset)
        """
        info = self.get_installed_plugin_info(plugin_name)
        if not info:
            debug(f"Plugin {plugin_name} is not installed")
            return None
        return info.versionstr
        
    def get_installed_manifests(self) -> List[Path]:
        """
        Return a list of all installed manifests
        """
        path = self.get_installed_manifests_path()
        if not path.exists():
            return []
        manifests = list(path.glob("*.json"))
        return manifests

    def get_installed_manifests_path(self) -> Path:
        """
        Returns the path to were installation manifests are saved in this system
        """
        return self.get_data_dir() / "installed/manifests"
        
    def get_data_dir(self) -> Path:
        """
        Return the data dir corresponding to risset
        """
        return _data_dir_for_platform() / "risset"

    def check_plugin_installed(self, plugin:Plugin) -> bool:
        # TODO
        return True

    def get_plugin_dll(self, plugin:Plugin) -> Union[Path, ErrorMsg]:
        """
        If the dll is a local file, just returns the absolute path,
        otherwise it downloads the binary to a temporary file and
        returns that path
        """
        binary_definition = plugin.binaries.get(self.platform)
        if not binary_definition:
            defined_platforms = ", ".join(plugin.binaries.keys())
            error = ErrorMsg(f"No binary defined for platform {self.platform}."
                             f" Available platforms for {plugin.name}: {defined_platforms}")
            return error
        # The manifest defines a path. If it is relative, it is relative to the
        # manifest itself.
        path = resolve_path(binary_definition.url, Path(plugin.source).parent.as_posix())
        if not path.exists():
            return ErrorMsg(f"Binary not found. Given path was: {str(path)}")
        return path

    def install_plugin(self, plugin: Plugin, user=False) -> Optional[ErrorMsg]:
        """
        Install the given plugin. Returns None if ok,
        an ErrorMsg if failed
        """
        debug("Installing plugin: ", plugin.name)
        if user:
            if not self.is_user_plugins_path_set():
                banner(self._user_plugins_path_message())
                return ErrorMsg("Asked to install in user path, but user path is not set")
            install_path = self.user_plugins_path
            sudo = False
        else:
            install_path = self.system_plugins_path
            sudo = True

        plugin_dll = self.get_plugin_dll(plugin)
        if isinstance(plugin_dll, ErrorMsg):
            error = plugin_dll
            return ErrorMsg(f"Could not find a binary for the given plugin: {error}")

        assert install_path is not None
        try:
            shutil.copy(plugin_dll.as_posix(), install_path.as_posix())
        except IOError as e:
            debug(str(e))
            if not sudo or not (self.platform == "linux" or self.platform == "macos"):
                return ErrorMsg("Could not copy the binary to the install path")
            error = _copy_with_sudo(plugin_dll.as_posix(), install_path.as_posix())
            if error:
                return error

        if not (install_path / plugin_dll).exists():
            return ErrorMsg(f"Installation of plugin {plugin.name} failed")

        # Installation succeeded, install manifest
        manifests_path = self.get_installed_manifests_path()
        if not manifests_path.exists():
            manifests_path.mkdir(parents=True)
        manifest = dataclasses.asdict(plugin)
        manifest['build_platform'] = plugin.binaries[self.platform].build_platform
        manifest_path = manifests_path / f"{plugin.name}.json"
        try:
            manifest_json = json.dumps(manifest, indent=True)
        except Exception as e:
            errormsg(str(e))
            return ErrorMsg("Error when dumping manifest to json")

        with open(manifest_path.as_posix(), "w") as f:
            f.write(manifest_json)
        debug(f"Saved manifest to {manifest_path}")
        debug(manifest_json)
        return None   # no errors

    def expand_plugin_glob(self, pattern) -> List[str]:
        """
        Given a glob pattern, match it against known plugins. Returns
        a list of matched plugins (the list might be empty)
        """
        return [plugin.name for plugin in self.plugins
                if fnmatch.fnmatch(plugin.name, pattern)]


class IndexParser:
    def __init__(self):
        """
        Create an index parser.
        The git repository holding the metadata/binaries is cloned/updated and
        the index defined in it is used. After that all files are accesses localy.
        """
        self.update_git_repository()
        self.index_folder: Path = self._get_path_of_git_repository()
        self.index: Path = self.index_folder / "plugins.json"
        assert self.index.exists()

    def _get_path_of_git_repository(self) -> Path:
        if sys.platform == 'linux':
            return Path("~/.local/share/risset/risset-data").expanduser()
        elif sys.platform == 'darwin':
            return Path("~/Library/Application Support/risset/risset-data").expanduser()
        elif sys.platform == 'win32':
            path = os.path.expandvars(R"C:\Users\$USERNAME\AppData\Local\risset\risset-data")
            return Path(path)
        else:
            raise RuntimeError(f"Platform {sys.platform} not supported")

    def update_git_repository(self) -> None:
        """
        Update the data repository. Clone if first time
        """
        gitpath = self._get_path_of_git_repository()
        if not gitpath.exists():
            _git_clone(GIT_REPOSITORY, gitpath)
        else:
            _git_update(gitpath)

    def parse(self) -> Union[PluginsIndex, ErrorMsg]:
        index_text = load_text(self.index.as_posix())
        return self._parse_index(index_text)

    def _parse_index(self, indexstr: str) -> Union[PluginsIndex, ErrorMsg]:
        """
        Parses the content of the plugins.json file

        Args:
            indexstr: the result of reading the plugins.json file
        """
        debug(f"Parsing index text: \n{indexstr}")
        try:
            d = json.loads(indexstr)
        except json.JSONDecodeError as e:
            debug("---- Could not parse index ----")
            debug(f"---- Index: \n{indexstr}")
            raise e

        # Check that it is a valid index (see DESIGN.md)
        plugins: Dict[str, str] = d.get('plugins')
        if plugins is None:
            raise ValueError("The plugins index does not have a 'plugins' key.")

        plugin_definitions: List[Plugin] = []
        for pluginkey, url in plugins.items():
            if is_url(url):
                raise ValueError("URLs are deprecated")
            result = plugin_definition_from_file(url, self.index_folder.as_posix())
            if isinstance(result, ErrorMsg):
                errormsg(f"Error parsing plugin {pluginkey}: {result}")
                continue
            plugin_definitions.append(result)
        index_version = d.get('version', '0.0.0')
        plugins_index = PluginsIndex(version=index_version, plugins=plugin_definitions)
        if plugins_index.system_plugins_path is None:
            return ErrorMsg("Could not find syste plugins folder")
        return plugins_index


###############################################################
#                        Subcommands                          #
###############################################################

def cmd_list(plugins_index:PluginsIndex, args):
    """
    Lists all plugins available for download
    """
    # TODO: implement flags: --json, --output
    leftcolwidth = 20
    for plugin in plugins_index.plugins:
        data = []
        extra_lines = []
        if plugins_index.platform not in plugin.binaries.keys():
            if not args.all:
                debug(f"Plugin {plugin.name} has no binary for platform {plugins_index.platform}")
                debug("    To include it in the list, use the --all flag")
                continue
            data.append("platform not supported")
        info = plugins_index.get_installed_plugin_info(plugin.name)
        if info:
            if info.versionstr == UNKNOWN_VERSION:
                data.append("installed (not by risset)")
            else:
                data.append(f"installed: {info.versionstr}")
            if not info.installed_in_system_folder:
                extra_lines.append(f"Path: {info.path}")
        if data:
            status = "[" + ", ".join(data) + "]"
        else:
            status = ""
        leftcol = f"{plugin.name}  @ {plugin.version}"
        print(f"* {leftcol.ljust(leftcolwidth)} | {plugin.short_description}  {status}")
        if extra_lines:
            for line in extra_lines:
                print(" "*leftcolwidth + f"   |   ", line)
    print()

def cmd_show(plugins_index: PluginsIndex, args) -> bool:
    """
    Returns True on success
    """
    plugin = args.plugin
    plugdef = plugins_index.find_plugin(plugin)
    if plugdef is None:
        errormsg(f"Plugin {plugin} unknown")
        return False
    installed_str = plugdef.version or "not installed"
    print()
    print(f"Plugin     : {plugdef.name}")
    print(f"Installed  : {installed_str}")
    print(f"Abstract   : {plugdef.short_description}")
    print(f"Minimal csound version : {plugdef.csound_version}")
    print(f"Author     : {plugdef.author}")
    print( "Platforms  : ")
    for platform, platform_info in plugdef.binaries.items():
        print(f"    * {platform}: {platform_info.build_platform}")
    print(f"Opcodes    :")
    opcstrs = textwrap.wrap(", ".join(plugdef.opcodes), 50)
    for s in opcstrs:
        print(" "*12, s)
    print()
    return True

def cmd_rm(plugins_index:PluginsIndex, args) -> bool:
    errors = []
    for plugin in args.plugin:
        plugdef = plugins_index.find_plugin(plugin)
        if plugdef is None:
            errors.append(f"Plugin {plugin} unknown")
            continue

        info = plugins_index.get_installed_plugin_info(plugin)
        if not info:
            errors.append(f"plugin {plugin} not installed, cannot remove")
            continue
        if not info.path.exists():
            errors.append(f"Could not find binary for plugin {plugin}. Declared binary: {str(info.path)}")
            continue

        removed = False
        try:
            os.remove(info.path.as_posix())
            removed = True
        except IOError as e:
            debug(str(e))
            if not info.installed_in_system_folder or not \
                    (plugins_index.platform == "linux" or plugins_index.platform == "macos"):
                errors.append("Could not copy the binary to the install path")
            else:
                error = _sudo_rm(info.path.as_posix())
                if error:
                    errors.append(error)
                else:
                    removed=True
        if removed:
            manifest_path = info.installed_manifest_path
            if manifest_path and manifest_path.exists():
                os.remove(manifest_path.as_posix())

    if not errors:
        return True

    for err in errors:
        errormsg(err)
    return False


def cmd_install(plugins_index:PluginsIndex, args) -> bool:
    """
    Install or upgrade a plugin

    If the plugin is installed but with a prior version, it will
    be upgraded. If it is installed but with an unknown version,
    installation is only performed if the --force flag is given

    Returns True if success

    Flags:
        --user   - install in user folder
        --force  - force installation even if plugin is already installed

    Args:
        plugin   - name of the plugin to install
    """
    allplugins = []
    for pattern in args.plugins:
        matched = plugins_index.expand_plugin_glob(pattern)
        if matched:
            allplugins.extend(matched)
    errors = []
    allplugins = list(set(allplugins))  # remove duplicates
    for plugin in allplugins:
        plugin_definition = plugins_index.find_plugin(plugin)
        if plugin_definition is None:
            errormsg(f"Plugin {plugin} unknown")
            return False
        current_version = plugins_index.get_plugin_installed_version(plugin)
        if current_version == UNKNOWN_VERSION:
            # plugin is installed but without a corresponding install manifest.
            if not args.force:
                errors.append(f"Plugin {plugin} is already installed. Use --force to force reinstall")
                errormsg(errors[-1])
                continue
        elif current_version is None:
            # plugin is not installed
            debug(f"Plugin {plugin} not installed, installing")
        else:
            if version_tuplet(plugin_definition.version) <= version_tuplet(current_version):
                debug(f"Plugin {plugin_definition.name}, version: {plugin_definition.version}")
                debug(f"    Installed version: {current_version}")
                errors.append(f"Installed version of plugin {plugin} is up-to-date")
                errormsg(errors[-1])
                continue
            debug(f"Updating plugin {args.plugin}: "
                  f"{current_version} -> {plugin_definition.version}")
        error = plugins_index.install_plugin(plugin_definition, user=args.user)
        if error:
            errors.append(error)
            errormsg(error)
    if errors:
        return False
    return True

def add_flag(parser, flag, help=""):
    parser.add_argument(flag, action="store_true", help=help)

def main():


    # Main parser
    parser = argparse.ArgumentParser()
    add_flag(parser, "--debug", help="Print debug information")
    add_flag(parser, "--version")
    subparsers = parser.add_subparsers(dest='command')

    # List command
    list_group = subparsers.add_parser('list', help="List packages")
    add_flag(list_group, "--json", help="Outputs list as json")
    add_flag(list_group, "--all", "List all plugins, even those without a binary for the current platform")
    list_group.add_argument("-o", "--outfile", help="Outputs to a file")
    list_group.set_defaults(func=cmd_list)

    # Install command
    install_group = subparsers.add_parser("install", help="Install a package")
    add_flag(install_group, "--user", help="Install in user folder")
    add_flag(install_group, "--force", help="Force install/reinstall")
    install_group.add_argument("plugins", nargs="+",
                               help="Name of the plugin/plugins to install. "
                                    "Glob pattern are supported (enclose them inside quotation marks)")
    install_group.set_defaults(func=cmd_install)

    # remove command
    rm_group = subparsers.add_parser("remove", help="Remove a package")
    rm_group.add_argument("plugin", nargs="+", help="Plugin/s to remove")
    rm_group.set_defaults(func=cmd_rm)

    # show command
    show_group = subparsers.add_parser("show", help="Show information about a plugin")
    show_group.add_argument("plugin", help="Plugin to gather information about")
    show_group.set_defaults(func=cmd_show)

    args = parser.parse_args()
    if args.debug:
        settings['debug'] = True

    if args.version:
        print(VERSION)
        sys.exit(0)

    index_parser = IndexParser()
    plugins_index = index_parser.parse()
    if isinstance(plugins_index, ErrorMsg):
        errormsg(plugins_index)
        sys.exit(-1)

    if not args.command:
        parser.print_help()
        sys.exit(-1)

    ok = args.func(plugins_index, args)
    if not ok:
        sys.exit(-1)

if __name__ == "__main__":
    main()