import logging

import importlib
import importlib.util
import os
import sys

from coilsnake.exceptions.common.exceptions import CoilSnakeError
from coilsnake.model.common.blocks import ROM_TYPE_NAME_UNKNOWN
from coilsnake.util.common.assets import open_asset
from coilsnake.util.common.yml import yml_load, yml_dump


log = logging.getLogger(__name__)

# This is a number which tells you the latest version number for the project
# format. Version numbers are necessary because the format of data files may
# change between versions of CoilSnake.

FORMAT_VERSION = 13

# Names for each version, corresponding the the CS version
VERSION_NAMES = {
    1:  "1.0",
    2:  "1.1",
    3:  "1.2",
    4:  "1.3",
    5:  "2.0.4",
    6:  "2.1",
    7:  "2.2",
    8:  "2.3.1",
    9:  "3.33",
    10: "4.0",
    11: "4.1",
    12: "4.2",
    13: "NEXT"
}

# The default project filename
PROJECT_FILENAME = "Project.snake"


def get_version_name(version):
    try:
        return VERSION_NAMES[version]
    except KeyError:
        return "Unknown Version"

class TempSysPathContext:
    def __init__(self, added_dir):
        self.added_dir = added_dir
        self.stored_path = None
    def __enter__(self):
        self.stored_path = sys.path
        new_path = [self.added_dir, *sys.path]
        sys.path = new_path
        importlib.invalidate_caches()
        return self
    def __exit__(self, *_args):
        sys.path = self.stored_path
        importlib.invalidate_caches()
        return False

class ModuleConfig:
    LABEL = 'module configuration'
    L_ENABLED = 'enabled modules'
    L_DISABLED = 'disabled modules'
    L_PROJ_SPECIFIC = 'project-specific modules'

    _DEFAULT_MODULES = None
    @classmethod
    def _get_default_modules(cls):
        if not cls._DEFAULT_MODULES:
            all_modules = []
            with open_asset("modulelist.txt") as f:
                for line in f:
                    line = line.rstrip('\n')
                    if line[0] == '#':
                        continue
                    components = line.split('.')
                    mod = __import__("coilsnake.modules." + line, globals(), locals(), [components[-1]])
                    all_modules.append((line, mod.__dict__[components[-1]]))
            cls._DEFAULT_MODULES = all_modules
        return cls._DEFAULT_MODULES

    def __init__(self, romtype, project_dir, moduleconfig_dict):
        self.romtype = romtype
        self.project_dir = project_dir
        if moduleconfig_dict:
            self.enabled_module_names = moduleconfig_dict[self.L_ENABLED]
            self.disabled_module_names = moduleconfig_dict[self.L_DISABLED]
            self.project_specific_modules = moduleconfig_dict[self.L_PROJ_SPECIFIC]
        else:
            self.enabled_module_names = self.get_compatible_default_module_names()
            self.disabled_module_names = []
            self.project_specific_modules = []

    def to_dict(self):
        return {
            self.L_ENABLED: self.enabled_module_names,
            self.L_DISABLED: self.disabled_module_names,
            self.L_PROJ_SPECIFIC: self.project_specific_modules,
        }

    def get_default_modules(self):
        return type(self)._get_default_modules()

    def class_is_compatible(self, cls):
        return cls.is_compatible_with_romtype(self.romtype)

    def filter_to_compatible(self, mods):
        return ((name, cls) for name, cls in mods if self.class_is_compatible(cls))

    def get_compatible_default_module_names(self):
        return [name for name, _ in self.filter_to_compatible(self.get_default_modules())]

    def add_missing_defaults(self, enabled=False):
        missing = []
        present = set(self.enabled_module_names) | set(self.disabled_module_names)
        for name, _ in self.filter_to_compatible(self.get_default_modules()):
            if name not in present:
                missing.append(name)
        if enabled:
            target_list = self.enabled_module_names
        else:
            target_list = self.disabled_module_names
        target_list += missing

    def get_project_modules(self):
        project_modules = []
        enabled = set(self.enabled_module_names)
        for name, cls in self.get_default_modules():
            if name in enabled and self.class_is_compatible(cls):
                project_modules.append((name, cls))
        for name in self.project_specific_modules:
            project_modules.append((name, self.load_project_specific_module(name)))
        return project_modules

    def load_project_specific_module(self, module_name):
        mod = sys.modules.get(module_name, None)
        if not mod:
            with TempSysPathContext(os.path.join(self.project_dir, "CustomModules")):
                spec = importlib.util.find_spec(module_name)
                if spec:
                    mod = importlib.import_module(module_name)
                    sys.modules[module_name] = mod
        if not mod:
            raise ValueError(f"Cannot locate module '{module_name}'")
        cls_name = module_name.rpartition('.')[-1]
        try:
            cls = mod.__dict__[cls_name]
        except KeyError as e:
            raise ValueError(f"Module '{module_name}' must have a class named '{cls_name}'") from e
        return cls

    def upgrade(self, old_version, new_version):
        if old_version < 13:
            # Ensure we're using the default values
            assert self.enabled_module_names == self.get_compatible_default_module_names()
            assert not self.disabled_module_names
            assert not self.project_specific_modules
        else:
            raise NotImplementedError(f"Don't know how to upgrade from version {old_version}")

class Project(object):
    def __init__(self, snake_filename, romtype=None):
        # Pre-initialize attributes
        self.romtype = ROM_TYPE_NAME_UNKNOWN
        self._resources = {}
        self.moduleconfig = None
        # Get snake filename and dirname
        assert isinstance(snake_filename, str)
        self.snake_filename = snake_filename
        self._dir_name = os.path.dirname(self.snake_filename)

        # Load project
        moduleconfig_dict = None
        try:
            with open(self.snake_filename, 'r') as f:
                data: dict = yml_load(f)
                if (romtype is None) or (romtype == data["romtype"]):
                    self.romtype = data["romtype"]
                    self._resources = data["resources"]
                    self.version = data.get("version", 1)
                    # This may not be present if we are opening a previous project version.
                    moduleconfig_dict = data.get(ModuleConfig.LABEL, None)

                    if self._resources is None:
                        self._resources = {}
                else:  # Loading a project of the wrong romtype
                    self.romtype = romtype
                    self._resources = {}
        except IOError:
            # Project file doesn't exist
            assert romtype and romtype != ROM_TYPE_NAME_UNKNOWN, "Can't make new project of unknown type!"
            self.romtype = romtype
            # Make parent directories if needed
            os.makedirs(self._dir_name, exist_ok=True)

        # Initialize moduleconfig based on information from project
        self.moduleconfig = ModuleConfig(self.romtype, self._dir_name, moduleconfig_dict)

    def save(self):
        # Dump non-resource values at start of file
        tmp = {
            'romtype': self.romtype,
            'version': FORMAT_VERSION,
            self.moduleconfig.LABEL: self.moduleconfig.to_dict(),
        }
        with open(self.snake_filename, 'w') as f:
            yml_dump(tmp, f)
            # Append resource list after other values
            yml_dump({'resources': self._resources}, f)

    def get_resource(self, module_name, resource_name, extension="dat", mode="r+", encoding=None, newline=None):
        if module_name not in self._resources:
            self._resources[module_name] = {}
        if resource_name not in self._resources[module_name]:
            self._resources[module_name][resource_name] = resource_name + "." + extension
        fname = os.path.join(self._dir_name, self._resources[module_name][resource_name])
        if not os.path.exists(os.path.dirname(fname)):
            os.makedirs(os.path.dirname(fname))
        f = open(fname, mode, encoding=encoding, newline=newline)
        return f

    def delete_resource(self, module_name, resource_name):
        if module_name not in self._resources:
            raise CoilSnakeError("No such module {}".format(module_name))
        if resource_name not in self._resources[module_name]:
            raise CoilSnakeError("No such resource {} in module {}".format(resource_name, module_name))
        fname = os.path.join(self._dir_name, self._resources[module_name][resource_name])
        if os.path.isfile(fname):
            os.remove(fname)
        del self._resources[module_name][resource_name]

    def load_modules(self):
        return self.moduleconfig.get_project_modules()

    def upgrade(self, old_version, new_version):
        self.moduleconfig.upgrade(old_version, new_version)
        self.version = new_version
