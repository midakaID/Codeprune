from parsers.lang_rules.base import LanguageRule, get_language_rules
from parsers.lang_rules.python_rules import PythonRules
from parsers.lang_rules.java_rules import JavaRules
from parsers.lang_rules.js_rules import JSRules
from parsers.lang_rules.c_rules import CRules

__all__ = [
    "LanguageRule", "get_language_rules",
    "PythonRules", "JavaRules", "JSRules", "CRules",
]
