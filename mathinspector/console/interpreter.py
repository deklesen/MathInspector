"""
Math Inspector: a visual programming environment for scientific computing
Copyright (C) 2021 Matt Calhoun

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""

import inspect, sys, traceback, os, re
import numpy as np
import tkinter as tk
from code import InteractiveInterpreter
from types import CodeType
from io import TextIOWrapper

from ..plot import plot
from ..doc import Help
from ..style import Color, TAGS
from ..util import vdict
from ..config import open_editor, BUTTON_RIGHT, BASEPATH, FONT, __version__
from ..widget import Text, Menu
from .builtin_print import builtin_print
from .codeparser import CodeParser
from .prompt import Prompt, FONTSIZE

RE_TRACEBACK = r"^()(Traceback \(most recent call last\))"
RE_EXCEPTION = r"^()([A-Za-z]*Error:)"
RE_FILEPATH = r"(File (\"(?!<).*\"))"
RE_INPUT = r"(File \"(<.*>)\")"
RE_LINE = r"((line [0-9]*))"
RE_IN = r"line ([0-9]*), in (.*)"

class Interpreter(Text, InteractiveInterpreter):
	"""
	This class extends `InteractiveInterpreter` from the module `code`,
	and follows the official patterns for creating python interpreters.

	In order to synchronize all the different views with the interpreter, the local namespace
	is stored in a vdict called `self.locals`.  A vdict is key-value pair, just like a regular
	dictionary, except it also has callbacks for events such as setting and deleting items.

	When running commands, `self.locals` is passed to the builtin `exec` function as the second parameter.
	This parameter determines the local namespace exec runs in.  You can display the current
	contents of the local namespace by running the command

	>>> locals()

	When exec processses code, it updates the values in `self.locals`, but since this is a vdict,
	the callbacks are automatically triggered.

	The math inspector console extends the traditional python interpreter, and has many quality of
	life improvements such as syntax highlighting and a wide variety of hotkeys. Before and
	after each command is executed, the command string is parsed to keep the other views
	synchronized with the variables in the local namespace.  Code parsing is done
	using the abstract syntax tree module `ast`.  An abstract syntax tree is generated by
	python under the hood as an intermediate step between processing
	lines of code and executing those commands.

	An excellent resource for learning about abstract syntax tree's is the
	website https://greentreesnakes.readthedocs.io/
	"""
	def __init__(self, app):
		InteractiveInterpreter.__init__(self, vdict({
			"__builtins__": __builtins__,
			"app": app,
			"plot": plot
		}, setitem=self.setitem, delitem=self.delitem))

		self.frame = tk.Frame(app,
			padx=16,
			pady=8,
			background=Color.DARK_BLACK)

		Text.__init__(self, self.frame,
			readonly=True,
			background=Color.DARK_BLACK,
			font=FONT,
			padx=0,
			pady=0,
			wrap="word",
			cursor="arrow",
			insertbackground=Color.DARK_BLACK)

		sys.stdout = StdWrap(sys.stdout, self.write) # stderr is overriden in __init__:main
		sys.excepthook = self.showtraceback

		__builtins__["help"] = Help(app)
		__builtins__["clear"] = Clear(self)
		__builtins__["license"] = License()
		__builtins__["copyright"] = Copyright()
		__builtins__["credits"] = Credits()

		self.app = app
		self.prompt = Prompt(self, self.frame)
		self.parse = CodeParser(app)
		self.buffer = []
		self.prevent_module_import = False
		self.cursor_position = self.index("end")

		self.bind("<Key>", self._on_key_log)
		self.bind("<Configure>", self.prompt.on_configure_log)
		self.bind("<ButtonRelease-1>", self.on_click_log)
		self.pack(fill="both", expand=True)

		for i in ["error_file"]:
			self.tag_bind(i, "<Motion>", lambda event, key=i: self._motion(event, key))
			self.tag_bind(i, "<Leave>", lambda event, key=i: self._leave(event, key))
			self.tag_bind(i, "<Button-1>", lambda event, key=i: self._click(event, key))

	def _init(self, event):
		self.config_count = self.config_count + 1 if hasattr(self, "config_count") else 1
		if self.config_count > 2:
			self.do_greet()
			self.prompt.place(width=self.winfo_width())
			self.bind("<Configure>", self.prompt.on_configure_log)

	def do_greet(self):
		self.write("Math Inspector " + __version__ + " (Beta)\nType \"help\", \"copyright\", \"credits\", \"license\" for more information")
		self.prompt()

	def setitem(self, key, value):
		if inspect.ismodule(value):
			if not self.prevent_module_import:
				self.app.modules[key] = value
		else:
			if self.prevent_module_import:
				if len(key) == 1 or key[:2] != "__":
					self.app.objects[key] = value
			else:
				self.app.objects[key] = value

	def delitem(self, key, value):
		if inspect.ismodule(value):
			del self.app.modules[key]
		else:
			del self.app.objects[key]

	def synclocals(self):
		self.locals.store.update(self.app.objects.store)
		self.locals.store.update(self.app.modules.store)

	def eval(self, source):
		self.synclocals()
		return eval(source, self.locals)

	def exec(self, source, filename="<file>"):
		self.prevent_module_import = True
		self.synclocals()
		self.parse.preprocess(source)
		self.runsource(source, filename, "exec")
		self.parse.postprocess(source)
		self.prevent_module_import = False

	def push(self, s, filename="<input>", log=True, symbol="single"):
		self.cursor_position = self.index("end-1l linestart")

		self.synclocals()
		source = "".join(self.buffer + [s])
		self.parse.preprocess(source)
		if source[:3] == "del" and source[4:] in self.app.objects:
			# NOTE - there is a strange issue with exec where it can't delete things from self.locals
			del self.app.objects[source[4:]]
			self.prompt()
			return
		elif source[:4] == "plot":
			self.prompt()
		did_compile = self.runsource(source, filename, symbol)

		if did_compile:
			self.buffer.append(s + "\n")
			self.prompt.history.append(s)
		else:
			self.parse.postprocess(source)
			self.prompt.history.extend(s)
			self.buffer.clear()
		self.prompt()

	def write(self, *args, syntax_highlight=False, tags=(), **kwargs):
		if self.prevent_module_import: return

		if len(args) == 1:
			if isinstance(args[0], np.ndarray):
				if not args[0].any():
					return
			elif args[0] in ("\a", "\n", " ", "", None):
				return


		idx = self.index("insert")
		for r in args:
			if isinstance(r, str) and "\r" in r:
				r = r.rsplit("\r", 1)[1]
				self.delete(self.cursor_position, "end")
				self.insert("end", "\n")

			if re.match(RE_TRACEBACK, str(r)) or re.match(RE_EXCEPTION, str(r)):
				tags = ("red", *tags)

			if r is not None:
				if isinstance(r, Exception):
					tags = tuple(list(tags) + ["red"])
				self.insert("end", str(r), tags, syntax_highlight=syntax_highlight)
				if len(args) > 1:
					self.insert("end", "\t")

		try:
			self.highlight(RE_FILEPATH, "error_file", idx)
			self.highlight(RE_INPUT, "purple", idx)
			self.highlight(RE_LINE, "blue", idx)
			self.highlight(RE_IN, "green", idx)
		except:
			pass

		if self.get("1.0", "end").strip():
			self.insert("end", "\n")

		self.see("end")
		self.prompt.move()

	def showtraceback(self, *args):
		sys.last_type, sys.last_value, last_tb = ei = sys.exc_info()
		sys.last_traceback = last_tb

		try:
			lines = traceback.format_exception(ei[0], ei[1], last_tb.tb_next)
			self.write(''.join(lines).rstrip(), tags="red")
			self.app.menu.setview("console", True)
		finally:
			last_tb = ei = None

	def clear(self):
		self.prompt.is_on_bottom = False
		self.delete("1.0", "end")

	def _on_key_log(self, event):
		result = self._on_key(event)
		if result:
			self.prompt.focus()
		return result

	def on_click_log(self, event):
		 if not self.tag_ranges("sel"):
		 	self.prompt.focus()

	def _on_button_right(self, event):
		option = []
		tag_ranges = self.tag_ranges("sel")
		if tag_ranges:
			option.extend([{
				"label": "Copy",
				"command": lambda: self.copy_to_clipboard(self.get(*tag_ranges))
			}])

		self.menu.show(event, option + [{
			"label": "Clear Log",
			"command": clear
		}])

	def _click(self, event, tag):
		if not self.hover_range: return
		content = self.get(*self.hover_range)
		if tag == "error_file":
			open_editor(self.app, os.path.abspath(content[1:-1]))


class StdWrap(TextIOWrapper):
    def __init__(self, buffer, write, **kwargs):
        super(StdWrap, self).__init__(buffer, **kwargs)
        self.write = write

class Copyright:
	def __repr__(self):
		return "Copyright (c) 2018-2021 Matt Calhoun.\nAll Rights Reserved."

class Credits:
	def __repr__(self):
		return """Created by Matt Calhoun.  Thanks to Casper da Costa-Luis for supporting
Math Inspector development, and to the contributors on GitHub.
See www.mathinspector.com for more information."""

class License:
	def __call__(self):
		help(os.path.join(BASEPATH, "LICENSE"))

	def __repr__(self):
		return __doc__ + "\n\nType license() to see the full license text"

class Clear:
	def __init__(self, console):
		self.console = console

	def __call__(self):
		self.console.clear()
		self.console.prompt()

	def __repr__(self):
		self.console.clear()
		self.console.prompt()
		return ""
