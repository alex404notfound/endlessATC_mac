// A persistent speech client. One JSON line in, one completion line out.
#import <AppKit/AppKit.h>
#include <stdio.h>

@interface ATCSpeechDelegate : NSObject <NSSpeechSynthesizerDelegate>
@property BOOL done;
@property BOOL success;
@end
@implementation ATCSpeechDelegate
- (void)speechSynthesizer:(NSSpeechSynthesizer *)sender didFinishSpeaking:(BOOL)success {
    self.success = success;
    self.done = YES;
}
@end

static NSString *voiceIdentifier(NSString *requested) {
    for (NSString *identifier in NSSpeechSynthesizer.availableVoices) {
        NSDictionary *attributes = [NSSpeechSynthesizer attributesForVoice:identifier];
        NSString *name = attributes[NSVoiceName];
        if ([identifier isEqualToString:requested] || [name isEqualToString:requested] ||
            [requested hasPrefix:[name stringByAppendingString:@" ("]]) return identifier;
    }
    return nil;
}

int main(void) {
    @autoreleasepool {
        NSSpeechSynthesizer *synth = [[NSSpeechSynthesizer alloc] initWithVoice:nil];
        ATCSpeechDelegate *delegate = [ATCSpeechDelegate new];
        synth.delegate = delegate;
        char *line = NULL;
        size_t capacity = 0;
        ssize_t length;
        while ((length = getline(&line, &capacity, stdin)) >= 0) {
            @autoreleasepool {
                BOOL ok = NO;
                NSString *failure = @"Invalid speech request";
                @try {
                    NSData *data = [NSData dataWithBytes:line length:(NSUInteger)length];
                    NSDictionary *request = [NSJSONSerialization JSONObjectWithData:data options:0 error:NULL];
                    if ([request isKindOfClass:NSDictionary.class] && [request[@"text"] isKindOfClass:NSString.class]) {
                        NSString *voice = [request[@"voice"] isKindOfClass:NSString.class] ? request[@"voice"] : nil;
                        NSString *identifier = voice.length ? voiceIdentifier(voice) : nil;
                        BOOL voiceOK = !voice.length || identifier != nil;
                        if (voiceOK && [synth setVoice:identifier]) {
                            synth.rate = [request[@"rate"] floatValue];
                            synth.volume = [request[@"volume"] floatValue];
                            delegate.done = NO;
                            if ([synth startSpeakingString:request[@"text"]]) {
                                while (!delegate.done) {
                                    [NSRunLoop.currentRunLoop runMode:NSDefaultRunLoopMode beforeDate:NSDate.distantFuture];
                                }
                                ok = delegate.success;
                            }
                            failure = @"Native speech could not finish";
                        } else {
                            failure = @"Native speech voice is unavailable";
                        }
                    }
                } @catch (NSException *exception) {
                    failure = exception.reason ?: @"Native speech failed";
                }
                NSDictionary *response = @{@"ok": @(ok), @"error": ok ? @"" : failure};
                NSData *encoded = [NSJSONSerialization dataWithJSONObject:response options:0 error:NULL];
                fwrite(encoded.bytes, 1, encoded.length, stdout);
                fputc('\n', stdout);
                fflush(stdout);
            }
        }
        [synth stopSpeaking];
        free(line);
    }
    return 0;
}
