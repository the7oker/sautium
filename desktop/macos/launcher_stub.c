/*
 * Bundle entry point: Contents/MacOS/Sautium.
 *
 * A Mach-O of our own rather than a shell script — LaunchServices, the Dock
 * and codesign all treat the main executable as the app's identity, and a
 * script main executable is the one shape that can neither be hardened-runtime
 * signed nor notarized later.
 *
 * It resolves its own bundle and hands over to the private CPython, which runs
 * bootstrap.py. execv, not fork: same PID means the same Dock tile, the same
 * app name, and no orphan process if the launcher outlives its parent.
 *
 * -B is load-bearing: a Python that caches bytecode next to the stdlib it
 * imports would write into a code-signed bundle, and a bundle whose files
 * changed after signing fails Gatekeeper with "a sealed resource is missing or
 * invalid". Everything past this process runs from the copy in the data root.
 */

#include <libgen.h>
#include <limits.h>
#include <mach-o/dyld.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

int main(int argc, char *argv[]) {
    char exe[PATH_MAX];
    uint32_t size = sizeof(exe);
    if (_NSGetExecutablePath(exe, &size) != 0) {
        fprintf(stderr, "Sautium: executable path too long\n");
        return 1;
    }

    char resolved[PATH_MAX];
    if (realpath(exe, resolved) == NULL) {
        perror("Sautium: realpath");
        return 1;
    }

    /* .../Contents/MacOS/Sautium -> .../Contents */
    char macos_dir[PATH_MAX];
    snprintf(macos_dir, sizeof(macos_dir), "%s", dirname(resolved));
    char contents[PATH_MAX];
    snprintf(contents, sizeof(contents), "%s", dirname(macos_dir));

    char python[PATH_MAX], bootstrap[PATH_MAX];
    snprintf(python, sizeof(python), "%s/Resources/runtime/bin/python3", contents);
    snprintf(bootstrap, sizeof(bootstrap), "%s/Resources/bootstrap.py", contents);

    char **args = calloc((size_t)argc + 3, sizeof(char *));
    if (args == NULL) {
        return 1;
    }
    args[0] = python;
    args[1] = "-B";
    args[2] = bootstrap;
    for (int i = 1; i < argc; i++) {
        args[i + 2] = argv[i];
    }

    execv(python, args);
    perror("Sautium: cannot start the bundled runtime");
    return 1;
}
