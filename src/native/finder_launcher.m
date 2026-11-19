#import <Foundation/Foundation.h>
#import <AppKit/AppKit.h>
#import <IOKit/ps/IOPowerSources.h>
#import <IOKit/ps/IOPSKeys.h>
#include <stdatomic.h>
#include <fcntl.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

extern int Py_Main(int argc, char **argv);

static atomic_int energySaving = 0;
static id simulationActivity = nil;

void ATCSetSimulationActive(int active) {
    if (active && simulationActivity == nil) {
        simulationActivity = [NSProcessInfo.processInfo
            beginActivityWithOptions:NSActivityUserInitiatedAllowingIdleSystemSleep
            reason:@"Visible air traffic simulation"];
    } else if (!active && simulationActivity != nil) {
        [NSProcessInfo.processInfo endActivity:simulationActivity];
        simulationActivity = nil;
    }
}

static void refreshPower(void *unused) {
    @autoreleasepool {
        CFTypeRef info = IOPSCopyPowerSourcesInfo();
        CFStringRef source = info ? IOPSGetProvidingPowerSourceType(info) : NULL;
        BOOL battery = source && CFEqual(source, CFSTR(kIOPSBatteryPowerValue));
        if (info) CFRelease(info);
        atomic_store(&energySaving, battery || NSProcessInfo.processInfo.lowPowerModeEnabled);
    }
}

// Read by the Python adapter. Notifications update the value, not frame polling.
int ATCEnergySavingMode(void) {
    return atomic_load(&energySaving);
}

int main(int argc, const char *argv[]) {
    @autoreleasepool {
        NSBundle *bundle = [NSBundle mainBundle];
        NSString *resources = [bundle resourcePath];
        if (resources == nil) {
            fprintf(stderr, "Endless ATC: app resources are missing.\n");
            return 1;
        }
        NSString *runtime = [resources stringByAppendingPathComponent:@"runtime"];
        NSString *loader = [resources stringByAppendingPathComponent:@"src/launcher.py"];
        NSString *payload = [resources stringByAppendingPathComponent:@"game/pythonscript.marshal"];
        NSString *game = [resources stringByAppendingPathComponent:@"game"];
        NSFileManager *manager = [NSFileManager defaultManager];

        unsetenv("PYTHONPATH");
        setenv("PYTHONHOME", [runtime fileSystemRepresentation], 1);
        setenv("PYGAME_HIDE_SUPPORT_PROMPT", "1", 1);
        setenv("SDL_APP_NAME", "Endless ATC", 0);
        setenv("ENDLESS_ATC_RETINA_SCALE", NSScreen.mainScreen.backingScaleFactor >= 2.0 ? "2" : "1", 0);

        NSMutableArray<NSString *> *arguments = [NSMutableArray arrayWithArray:@[
            bundle.executablePath, @"-s", @"-B", @"-O", @"-u", loader,
            @"--game-dir", game, @"--payload", payload
        ]];

        for (int index = 1; index < argc; index++) {
            NSString *argument = [NSString stringWithUTF8String:argv[index]];
            if (argument != nil && ![argument hasPrefix:@"-psn_"]) {
                [arguments addObject:argument];
            }
        }

        if (!isatty(STDERR_FILENO) && ![arguments containsObject:@"--smoke"]) {
            NSString *data = [NSProcessInfo processInfo].environment[@"ENDLESS_ATC_DATA_DIR"];
            if (data.length == 0) {
                data = [NSHomeDirectory() stringByAppendingPathComponent:
                        @"Library/Application Support/Endless ATC"];
            }
            [manager createDirectoryAtPath:data withIntermediateDirectories:YES
                                attributes:nil error:NULL];
            NSString *log = [data stringByAppendingPathComponent:@"launch.log"];
            int fd = open([log fileSystemRepresentation], O_WRONLY | O_CREAT | O_TRUNC, 0600);
            if (fd >= 0) {
                dup2(fd, STDOUT_FILENO);
                dup2(fd, STDERR_FILENO);
                close(fd);
            }
        }

        char **command = calloc(arguments.count + 1, sizeof(char *));
        if (command == NULL) {
            return 1;
        }
        for (NSUInteger index = 0; index < arguments.count; index++) {
            command[index] = strdup([arguments[index] fileSystemRepresentation]);
            if (command[index] == NULL) {
                return 1;
            }
        }
        refreshPower(NULL);
        CFRunLoopSourceRef powerSource = IOPSNotificationCreateRunLoopSource(refreshPower, NULL);
        if (powerSource) CFRunLoopAddSource(CFRunLoopGetMain(), powerSource, kCFRunLoopCommonModes);
        id powerObserver = [NSNotificationCenter.defaultCenter
            addObserverForName:NSProcessInfoPowerStateDidChangeNotification object:nil queue:nil
            usingBlock:^(NSNotification *note) { refreshPower(NULL); }];
        // Do not hold a process-wide user-initiated activity for the entire game.
        // Let macOS apply its normal energy heuristics when the app is hidden.
        int status = Py_Main((int)arguments.count, command);
        ATCSetSimulationActive(0);
        [NSNotificationCenter.defaultCenter removeObserver:powerObserver];
        if (powerSource) {
            CFRunLoopRemoveSource(CFRunLoopGetMain(), powerSource, kCFRunLoopCommonModes);
            CFRelease(powerSource);
        }
        for (NSUInteger index = 0; index < arguments.count; index++) free(command[index]);
        free(command);
        return status;
    }
}
