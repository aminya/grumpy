#!/usr/bin/env python
# coding=utf-8

# Copyright 2016 Google Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""A Python -> Go transcompiler."""

from __future__ import unicode_literals

import argparse
import os
import sys
from StringIO import StringIO
import textwrap

from .compiler import block
from .compiler import imputil
from .compiler import stmt
from .compiler import util
from .vendor import pythonparser
from .pep_support.pep3147pycache import make_transpiled_module_folders
from . import pydeps


def main(stream=None, modname=None, pep3147=False, recursive=False):
  script = os.path.abspath(stream.name)
  assert script and modname, 'Script "%s" or Modname "%s" is empty' % (script,modname)

  gopath = os.getenv('GOPATH', None)
  if not gopath:
    raise RuntimeError('GOPATH not set')

  pep3147_folders = make_transpiled_module_folders(script, modname)

  stream.seek(0)
  py_contents = stream.read()
  mod = pythonparser.parse(py_contents)

  # Do a pass for compiler directives from `from __future__ import *` statements
  future_node, future_features = imputil.parse_future_features(mod)

  importer = imputil.Importer(gopath, modname, script,
                              future_features.absolute_import)
  full_package_name = modname.replace('.', '/')
  mod_block = block.ModuleBlock(importer, full_package_name, script,
                                py_contents, future_features)

  visitor = stmt.StatementVisitor(mod_block, future_node)
  # Indent so that the module body is aligned with the goto labels.
  with visitor.writer.indent_block():
    visitor.visit(mod)

  if os.path.exists(script):
    deps, import_objects = pydeps.main(script, modname, with_imports=True) #, script, gopath)
  elif os.path.exists(os.path.join(pep3147_folders['cache_folder'], os.path.basename(script))):
    deps, import_objects = pydeps.main(
      os.path.join(pep3147_folders['cache_folder'], os.path.basename(script)),
      modname,
      package_dir=os.path.dirname(script),
      with_imports=True,
    )
  else:
    raise NotImplementedError()

  deps = set(deps).difference(_get_parent_packages(modname))

  imports = ''.join('\t_ "' + _package_name(name) + '"\n' for name in deps)
  if recursive:
    for imp_obj in import_objects:
      if not imp_obj.is_native:
        # Recursively compile the discovered imports
        # TODO: Fix cyclic imports?
        name = imp_obj.name[1:] if imp_obj.name.startswith('.') else imp_obj.name
        main(stream=open(imp_obj.script), modname=name, pep3147=True, recursive=True)

  file_buffer = StringIO()
  writer = util.Writer(file_buffer)
  tmpl = textwrap.dedent("""\
      package $package
      import (
      \tπg "grumpy"
      $imports
      )
      var Code *πg.Code
      func init() {
      \tCode = πg.NewCode("<module>", $script, nil, 0, func(πF *πg.Frame, _ []*πg.Object) (*πg.Object, *πg.BaseException) {
      \t\tvar πR *πg.Object; _ = πR
      \t\tvar πE *πg.BaseException; _ = πE""")
  writer.write_tmpl(tmpl, package=modname.split('.')[-1],
                    script=util.go_str(script), imports=imports)
  with writer.indent_block(2):
    for s in sorted(mod_block.strings):
      writer.write('ß{} := πg.InternStr({})'.format(s, util.go_str(s)))
    writer.write_temp_decls(mod_block)
    writer.write_block(mod_block, visitor.writer.getvalue())
  writer.write_tmpl(textwrap.dedent("""\
    \t\treturn nil, πE
    \t})
    \tπg.RegisterModule($modname, Code)
    }"""), modname=util.go_str(modname))

  if pep3147:
    file_buffer.seek(0)
    new_gopath = pep3147_folders['gopath_folder']
    if new_gopath not in os.environ['GOPATH'].split(os.pathsep):
      os.environ['GOPATH'] += os.pathsep + new_gopath

    mod_dir = pep3147_folders['transpiled_module_folder']
    with open(os.path.join(mod_dir, 'module.go'), 'w+') as transpiled_file:
      transpiled_file.write(file_buffer.read())
  file_buffer.seek(0)
  return file_buffer.read()


def _package_name(modname):
  if modname.startswith('__go__/'):
    return '__python__/' + modname
  return '__python__/' + modname.replace('.', '/')


def _get_parent_packages(modname):
  package_parts = modname.split('.')
  parent_parts = package_parts[:-1]
  parent_packages = set()
  for i, _ in enumerate(parent_parts):
    yield '.'.join(parent_parts[:(-i or None)])
