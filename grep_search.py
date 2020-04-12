# pylint: disable=no-else-raise
# pylint: disable=no-else-return
# pylint: disable=no-else-continue

import os.path
from collections import defaultdict
import functools
import subprocess
import pathlib

import sublime  # pylint: disable=import-error
import sublime_plugin  # pylint: disable=import-error

SEARCH_INPUT_PROMPT_STR = "Search:"

# Trim lines to no more than this in the results quick panel. Without trimming,
# Sublime might hang on long lines - often encountered in minified Javascript,
# for example.
MAX_RESULT_LINE_LENGTH = 1000

REGION_KEY = "grep_search"

HOME = os.path.expanduser('~')

# Settings.

def get_settings():
    return sublime.load_settings('GrepSearch.sublime-settings')

def get_engine_name():
    return get_settings().get("engine_name")

def get_executable_path():
    return get_settings().get("executable_path")

def get_required_args():
    return get_settings().get("required_args")

def get_show_list_by_default():
    return get_settings().get('show_list_by_default')

# /Settings.

# Utilities.

def search_folders_for_window(window):
    window_folders = window.folders()
    if window_folders:
        return window_folders
    else:
        filename = window.active_view().file_name()
        if filename:
            return [os.path.dirname(filename)]
        else:
            sublime.error_message('No folders to search in')
            raise ValueError('No folders to search in')

# Abbreviate some prefixes to render paths more nicely.
def shorten_path(path, heres):
    path_path = pathlib.Path(path)
    abbrevs = [(here, '.') for here in heres] + [(HOME, '~')]
    for prefix, abbrev in abbrevs:
        try:
            return str(abbrev / path_path.relative_to(prefix))
        except ValueError:
            continue

def render_location(match):
    return ':'.join([match['path'], str(match['line_nr']), str(match['col_nr'])])

# /Utilities.

# Helper to deal with operating on views once loaded. Sorry for the mutable
# global state.

callbacks_on_view_loaded = {}

class OnLoadedCallbackEventListener(sublime_plugin.ViewEventListener):  # pylint: disable=too-few-public-methods

    def on_load_async(self):
        try:
            f = callbacks_on_view_loaded.pop(self.view.id())
        except KeyError:
            pass
        else:
            f(self.view)

    def on_load(self):
        self.on_load_async()

# /Callback helper.

# Command proper.

class GrepSearchCommand(sublime_plugin.WindowCommand):

    def run(self, immediate=False, search_mode='plain'):
        view = self.window.active_view()

        selected_text = view.substr(view.sel()[0])

        initial_query = (
            selected_text
            if selected_text and '\n' not in selected_text
            else ''
        )

        if immediate:
            if not initial_query:
                sublime.error_message('Nothing selected to search')
            else:
                self.search_and_display(search_mode, initial_query)
        else:
            self.window.show_input_panel(
                SEARCH_INPUT_PROMPT_STR,
                initial_query,
                on_done=functools.partial(self.search_and_display, search_mode),
                on_change=None,
                on_cancel=None
            )

    def search_and_display(self, search_mode, query):
        original_view = self.window.active_view()
        folders = search_folders_for_window(self.window)

        if search_mode == 'plain':
            matches = list(remove_similar_matches(
                search(query, folders),
                ignore_adjacents=False
            ))

        elif search_mode == 'haskell':
            matches = list(remove_similar_matches(
                search_for_haskell_defns(query, folders),
                ignore_adjacents=True,
            ))
        else:
            sublime.error_message(f'Unknown search mode: {search_mode}')

        if not matches:
            sublime.message_dialog('No results')
        else:
            if get_show_list_by_default():
                self.list_in_view(query, matches)
            else:
                display_results = [
                    [
                        shorten_path(render_location(match), folders),
                        match['content'].strip()[:MAX_RESULT_LINE_LENGTH],
                    ]
                    for match in matches
                ] + ["``` List results in view ```"]

                self.on_highlight(query, matches, 0)
                self.window.show_quick_panel(
                    display_results,
                    functools.partial(
                        self.on_done,
                        original_view,
                        query,
                        matches
                    ),
                    flags=sublime.KEEP_OPEN_ON_FOCUS_LOST,
                    on_highlight=functools.partial(
                        self.on_highlight,
                        query,
                        matches
                    ),
                )

    def on_highlight(self, query, matches, file_nr):
        if file_nr not in (-1, len(matches)):
            self.open_and_highlight_file(query, matches[file_nr], transient=True)

    def open_and_highlight_file(self, query, match, transient):
        view = self.window.open_file(
            render_location(match),
            flags=(
                sublime.ENCODED_POSITION
                | sublime.FORCE_GROUP
                | (sublime.TRANSIENT if transient else 0)
            ),
        )

        def annotate_view(v):
            v.add_regions(
                key=REGION_KEY,
                regions=v.find_all(query, sublime.IGNORECASE),
                scope="entity.name.filename.find-in-files",
                icon="circle",
                flags=sublime.DRAW_OUTLINED,
            )

        # Can't operate on view while it's loading, so set a callback to be run
        # by `OnLoadedCallbackEventListener`.
        if view.is_loading():
            callbacks_on_view_loaded[view.id()] = annotate_view
        else:
            annotate_view(view)


    def on_done(self, original_view, query, matches, file_nr):
        # Cancelled.
        if file_nr == -1:
            self.window.focus_view(original_view)
        # Last result is "list in view", at one past the last index of
        # data-results.
        elif file_nr == len(matches):
            self.list_in_view(query, matches)
        else:
            self.open_and_highlight_file(query, matches[file_nr], transient=False)
        self.clear_markup()

    def clear_markup(self):
        for view in self.window.views():
            view.erase_regions(REGION_KEY)

    def list_in_view(self, query, matches):
        self.window.new_file().run_command('grep_search_results', {
            'matches': matches,
            'query': query,
        })

def remove_similar_matches(matches, ignore_adjacents):
    matches = list(matches)
    # Always yield the first value, if there are any values at all.
    if matches:
        yield matches[0]
    for i in range(1, len(matches)):
        prev_match = matches[i - 1]
        this_match = matches[i]

        if this_match['path'] == prev_match['path']:
            if this_match['line_nr'] == prev_match['line_nr']:
                continue

            # Results that span multiple lines will appear as multiple results,
            # one per line. To avoid this, remove all but the first result when
            # we have results on adjacent lines. This will have some false
            # positives (we will ignore truly distinct results), but the
            # proximity of the results should reduce this cost.
            if ignore_adjacents and (this_match['line_nr'] == prev_match['line_nr'] + 1):
                continue

        yield this_match

# /Command proper.

# Display results in a view.

class GrepSearchResultsCommand(sublime_plugin.TextCommand):  # pylint: disable=too-few-public-methods

    def run(self, edit, matches, query):
        self.view.set_name('Find Results')
        self.view.set_scratch(True)
        self.view.set_syntax_file('Packages/Default/Find Results.hidden-tmLanguage')
        self.view.insert(
            edit,
            self.view.text_point(0, 0),
            render_matches(matches, query)
        )
        self.view.sel().clear()
        self.view.sel().add(sublime.Region(0, 0))

def render_path_matches(path, matches):
    rendered_matches = "\n".join(
        "  {l}:{c} {s}".format(l=m['line_nr'], c=m['col_nr'], s=m['content'])
        for m in matches
    )
    return "{}:\n{}".format(path, rendered_matches)

def render_matches(matches, query):
    matches_by_path = defaultdict(list)
    for match in matches:
        path = match.pop('path')
        matches_by_path[path].append(match)
    match_count = len(matches)
    path_count = len(matches_by_path)

    intro = (
        f"GrepSearch matches for \"{query}\" ({match_count} lines in {path_count} files):"
    )

    body = "\n".join(
        render_path_matches(path, path_matches)
        for path, path_matches in matches_by_path.items()
    )
    return intro + "\n\n" + body

# /Display results in a view.

# Gnarly search implementations.

engines_needing_error_output = {
    'ripgrep',
    'the_platinum_searcher',
    'the_silver_searcher',
}

def search(query, folders):
    """
    Run the search engine. Return a list of tuples, where first element is
    the absolute file path, and optionally row information, separated
    by a semicolon, and the second element is the result string
    """
    if not query.strip():
        return

    cmd = [get_executable_path()] + get_required_args() + [query] + folders

    print(f"Running: {' '.join(cmd)}")
    completed_process = subprocess.run(
        cmd,
        capture_output=True,
        # Seconds.
        timeout=10,
        cwd=folders[0],
        text=True,
        # We will check the return code ourselves, it's not so simple.
        check=False,
    )
    output, error = completed_process.stdout.strip(), completed_process.stderr.strip()

    if get_engine_name() in engines_needing_error_output:
        is_error = completed_process.returncode != 0 and error != ""
    else:
        is_error = completed_process.returncode != 0
    if is_error:
        raise ValueError(error)

    for line in output.split("\n"):
        if not line.strip():
            continue
        parts = line.split(":", 3)
        if len(parts) < 4:
            raise ValueError(f'Too few colon-separated section in match: {line}')
        yield dict(
            path=parts[0].strip(),
            line_nr=int(parts[1]),
            col_nr=int(parts[2]),
            content=parts[3],
        )

# Search for Haskell definitions of various kinds.
def search_for_haskell_defns(query, folders):
    query = query.strip()

    # Decide what to look for based on whether query starts with an uppercase
    # letter.
    if query[0].isalpha() and query[0].upper() == query[0]:
        first_constructor_query = r'(data|newtype) .+\s* =\s* {}\s'.format(query)
        later_constructor_query = r'\|\s* {}\s'.format(query)

        queries = [
            (
                'Module',
                r'^module +({})\s'.format(query),
            ),
            (
                'Type definition',
                r'^(data|newtype|type)\s+{}(\s+[a-z]+)*\s+(=|where)\s'.format(query),
            ),
            (
                'Class definition',
                r'^class\s+([a-zA-Z\s.\(\),]+=>\s+)?{}(\s+[a-z]+)*\s+where\s'.format(query),
            ),
            (
                'Constructor',
                '({})|({})'.format(first_constructor_query, later_constructor_query),
            ),
        ]
    else:
        queries = [
            (
                'Value type',
                r'^ *({})\s+::\s+.'.format(query),
            ),

            (
                'Value definition',
                r'^ *({})\s+[^=$]*\s=\s'.format(query),
            ),

            (
                'Record getter',
                r' +[{{,] +({})\s+::\s+'.format(query),
            ),
        ]
    matches = []
    for _, sub_query in queries:
        sub_query_matches = search(sub_query, folders)
        # query_matches_annotated = [
        #     (p, '{}: {}'.format(query_name, c))
        #     for p, c in sub_query_matches
        # ]
        # matches.extend(query_matches_annotated)
        matches.extend(sub_query_matches)
    return matches

# /Gnarly search implementations.
