#import <Cocoa/Cocoa.h>
#import <CommonCrypto/CommonDigest.h>
#import <CoreGraphics/CoreGraphics.h>

static NSString *const kManifestRelativePath = @"Library/Application Support/Codex/Daily Briefing/wallpaper_hotspots.json";
static NSString *const kWallpaperStatusRelativePath = @"Library/Application Support/Codex/Daily Briefing/wallpaper_status.json";
static NSString *const kOverlayStatusRelativePath = @"Library/Application Support/Codex/Daily Briefing/wallpaper_hotspot_overlay_status.json";
static NSString *const kReportRelativePath = @"Desktop/My_Planner.html";
static NSString *const kDispatcherRelativePath = @"Library/Application Support/Codex/Daily Briefing/daily_briefing_dispatcher.py";
static NSString *const kInternDispatcherRelativePath = @"Library/Application Support/Codex/Daily Briefing/intern_application_dispatcher.py";
static NSString *const kPythonPath = @"/Library/Frameworks/Python.framework/Versions/3.12/bin/python3";

@interface PlannerHotspotPanel : NSPanel
@end

@implementation PlannerHotspotPanel
- (BOOL)canBecomeKeyWindow { return NO; }
- (BOOL)canBecomeMainWindow { return NO; }
@end

@interface PlannerHotspotView : NSView
@property(nonatomic, strong, nullable) NSURL *remoteURL;
@property(nonatomic, strong) NSURL *localReportURL;
@property(nonatomic, strong) NSURL *internDispatcherURL;
@property(nonatomic, copy) NSString *accessibleLabel;
@property(nonatomic, copy) NSString *localAction;
@property(nonatomic, strong) NSImage *wallpaperImage;
@property(nonatomic) NSRect wallpaperSourceRect;
@property(nonatomic) BOOL hovered;
@property(nonatomic) BOOL linkModeActive;
@property(nonatomic, strong, nullable) NSTrackingArea *trackingArea;
- (instancetype)initWithFrame:(NSRect)frame
                          item:(NSDictionary *)item
                localReportURL:(NSURL *)localReportURL
                wallpaperImage:(NSImage *)wallpaperImage
              wallpaperCanvas:(NSSize)wallpaperCanvas;
@end

@implementation PlannerHotspotView

- (instancetype)initWithFrame:(NSRect)frame
                          item:(NSDictionary *)item
                localReportURL:(NSURL *)localReportURL
                wallpaperImage:(NSImage *)wallpaperImage
              wallpaperCanvas:(NSSize)wallpaperCanvas {
    self = [super initWithFrame:frame];
    if (self) {
        _localReportURL = localReportURL;
        NSURL *home = NSFileManager.defaultManager.homeDirectoryForCurrentUser;
        _internDispatcherURL = [home URLByAppendingPathComponent:kInternDispatcherRelativePath];
        _accessibleLabel = [item[@"label"] isKindOfClass:[NSString class]] ? item[@"label"] : @"My Planner item";
        _localAction = [item[@"action"] isKindOfClass:[NSString class]] ? item[@"action"] : @"";
        NSString *rawURL = [item[@"url"] isKindOfClass:[NSString class]] ? item[@"url"] : nil;
        if (rawURL.length > 0) {
            NSURLComponents *components = [NSURLComponents componentsWithString:rawURL];
            NSString *scheme = components.scheme.lowercaseString;
            if (([scheme isEqualToString:@"https"] || [scheme isEqualToString:@"http"]) &&
                components.host.length > 0 && components.user.length == 0 && components.password.length == 0) {
                _remoteURL = components.URL;
            }
        }
        NSDictionary *rect = [item[@"rect"] isKindOfClass:[NSDictionary class]] ? item[@"rect"] : @{};
        CGFloat sourceX = [rect[@"x"] doubleValue];
        CGFloat sourceYFromTop = [rect[@"y"] doubleValue];
        CGFloat sourceWidth = [rect[@"width"] doubleValue];
        CGFloat sourceHeight = [rect[@"height"] doubleValue];
        _wallpaperImage = wallpaperImage;
        _wallpaperSourceRect = NSMakeRect(sourceX,
                                          wallpaperCanvas.height - sourceYFromTop - sourceHeight,
                                          sourceWidth,
                                          sourceHeight);
        _linkModeActive = NO;
        self.wantsLayer = YES;
        self.layer.backgroundColor = NSColor.clearColor.CGColor;
        [self setAccessibilityElement:YES];
        [self setAccessibilityRole:NSAccessibilityButtonRole];
        [self setAccessibilityLabel:_accessibleLabel];
    }
    return self;
}

- (BOOL)isOpaque { return NO; }
- (BOOL)acceptsFirstMouse:(NSEvent *)event { (void)event; return YES; }

- (void)setLinkModeActive:(BOOL)linkModeActive {
    if (_linkModeActive == linkModeActive) {
        return;
    }
    _linkModeActive = linkModeActive;
    if (!linkModeActive) {
        _hovered = NO;
    }
    [self.window invalidateCursorRectsForView:self];
    self.needsDisplay = YES;
}

- (void)updateTrackingAreas {
    if (self.trackingArea != nil) {
        [self removeTrackingArea:self.trackingArea];
    }
    self.trackingArea = [[NSTrackingArea alloc]
        initWithRect:NSZeroRect
             options:NSTrackingMouseEnteredAndExited | NSTrackingActiveAlways | NSTrackingInVisibleRect
               owner:self
            userInfo:nil];
    [self addTrackingArea:self.trackingArea];
    [super updateTrackingAreas];
}

- (void)resetCursorRects {
    if (self.linkModeActive) {
        [self addCursorRect:self.bounds cursor:NSCursor.pointingHandCursor];
    }
}

- (void)mouseEntered:(NSEvent *)event {
    (void)event;
    self.hovered = YES;
    self.needsDisplay = YES;
}

- (void)mouseExited:(NSEvent *)event {
    (void)event;
    self.hovered = NO;
    self.needsDisplay = YES;
}

- (void)mouseDown:(NSEvent *)event {
    (void)event;
}

- (void)mouseUp:(NSEvent *)event {
    // Require Option through mouse-up and keep the pointer inside the card.
    // This turns activation into a deliberate click and closes key/timer races.
    NSPoint point = [self convertPoint:event.locationInWindow fromView:nil];
    if ((event.modifierFlags & NSEventModifierFlagOption) == 0 ||
        !NSPointInRect(point, self.bounds)) {
        return;
    }
    [self openDestination];
}

- (BOOL)accessibilityPerformPress {
    [self openDestination];
    return YES;
}

- (void)openDestination {
    if ([self.localAction isEqualToString:@"start_intern_application_batch"]) {
        if (self.localReportURL != nil) {
            [[NSWorkspace sharedWorkspace] openURL:self.localReportURL];
        }
        if ([NSFileManager.defaultManager fileExistsAtPath:kPythonPath] &&
            [NSFileManager.defaultManager fileExistsAtPath:self.internDispatcherURL.path]) {
            NSTask *task = [[NSTask alloc] init];
            task.executableURL = [NSURL fileURLWithPath:kPythonPath];
            task.arguments = @[self.internDispatcherURL.path, @"--from-wallpaper"];
            task.standardOutput = [NSFileHandle fileHandleWithNullDevice];
            task.standardError = [NSFileHandle fileHandleWithNullDevice];
            @try {
                [task launchAndReturnError:nil];
            } @catch (__unused NSException *exception) {
            }
        }
        return;
    }
    NSURL *destination = self.remoteURL ?: self.localReportURL;
    if (destination != nil) {
        [[NSWorkspace sharedWorkspace] openURL:destination];
    }
}

- (void)drawRect:(NSRect)dirtyRect {
    [super drawRect:dirtyRect];
    if (!self.linkModeActive) {
        return;
    }

    // While link mode is active, redraw this card's exact wallpaper crop above
    // Finder's desktop icons. This makes the card readable even when a file is
    // parked on the same pixels; releasing Option restores the normal desktop.
    if (self.wallpaperImage != nil && !NSIsEmptyRect(self.wallpaperSourceRect)) {
        [self.wallpaperImage drawInRect:self.bounds
                               fromRect:self.wallpaperSourceRect
                              operation:NSCompositingOperationCopy
                               fraction:1.0
                         respectFlipped:NO
                                  hints:@{NSImageHintInterpolation: @(NSImageInterpolationHigh)}];
    }
    NSBezierPath *path = [NSBezierPath bezierPathWithRoundedRect:NSInsetRect(self.bounds, 2.0, 2.0)
                                                         xRadius:13.0
                                                         yRadius:13.0];
    CGFloat fillAlpha = self.hovered ? 0.13 : 0.055;
    CGFloat strokeAlpha = self.hovered ? 0.98 : 0.78;
    [[NSColor colorWithSRGBRed:0.31 green:0.69 blue:1.0 alpha:fillAlpha] setFill];
    [path fill];
    [[NSColor colorWithSRGBRed:0.40 green:0.76 blue:1.0 alpha:strokeAlpha] setStroke];
    path.lineWidth = self.hovered ? 2.4 : 1.5;
    [path stroke];
}

@end


@interface PlannerAppDelegate : NSObject <NSApplicationDelegate>
@property(nonatomic, strong) NSURL *manifestURL;
@property(nonatomic, strong) NSURL *wallpaperStatusURL;
@property(nonatomic, strong) NSURL *overlayStatusURL;
@property(nonatomic, strong) NSURL *localReportURL;
@property(nonatomic, strong) NSURL *dispatcherURL;
@property(nonatomic, strong) NSMutableArray<NSPanel *> *panels;
@property(nonatomic, strong, nullable) NSTimer *reloadTimer;
@property(nonatomic, strong, nullable) NSTimer *modifierTimer;
@property(nonatomic, strong, nullable) NSDate *manifestModificationDate;
@property(nonatomic, strong, nullable) NSDate *wallpaperStatusModificationDate;
@property(nonatomic, strong, nullable) NSDate *lastCatchupRequest;
@property(nonatomic, strong, nullable) NSDate *lastOptionPress;
@property(nonatomic, copy) NSDictionary *lastOverlayStatus;
@property(nonatomic) BOOL linkModeActive;
@property(nonatomic) BOOL validateOnly;
@end

@implementation PlannerAppDelegate

- (void)applicationDidFinishLaunching:(NSNotification *)notification {
    (void)notification;
    NSURL *home = NSFileManager.defaultManager.homeDirectoryForCurrentUser;
    self.manifestURL = [home URLByAppendingPathComponent:kManifestRelativePath];
    self.wallpaperStatusURL = [home URLByAppendingPathComponent:kWallpaperStatusRelativePath];
    self.overlayStatusURL = [home URLByAppendingPathComponent:kOverlayStatusRelativePath];
    self.localReportURL = [home URLByAppendingPathComponent:kReportRelativePath];
    self.dispatcherURL = [home URLByAppendingPathComponent:kDispatcherRelativePath];
    self.panels = [NSMutableArray array];
    self.lastCatchupRequest = [NSDate date];
    self.linkModeActive = NO;

    [self reloadHotspots];
    if (self.validateOnly) {
        NSData *json = [NSJSONSerialization dataWithJSONObject:self.lastOverlayStatus ?: @{} options:0 error:nil];
        if (json != nil) {
            fwrite(json.bytes, 1, json.length, stdout);
            fwrite("\n", 1, 1, stdout);
        }
        [NSApp terminate:nil];
        return;
    }

    [NSNotificationCenter.defaultCenter addObserver:self
                                            selector:@selector(displayConfigurationChanged:)
                                                name:NSApplicationDidChangeScreenParametersNotification
                                              object:nil];
    NSNotificationCenter *workspaceCenter = NSWorkspace.sharedWorkspace.notificationCenter;
    [workspaceCenter addObserver:self selector:@selector(desktopContextChanged:)
                            name:NSWorkspaceActiveSpaceDidChangeNotification object:nil];
    [workspaceCenter addObserver:self selector:@selector(sessionBecameActive:)
                            name:NSWorkspaceDidWakeNotification object:nil];
    [workspaceCenter addObserver:self selector:@selector(sessionBecameActive:)
                            name:NSWorkspaceSessionDidBecomeActiveNotification object:nil];
    [workspaceCenter addObserver:self selector:@selector(sessionBecameInactive:)
                            name:NSWorkspaceWillSleepNotification object:nil];
    [workspaceCenter addObserver:self selector:@selector(sessionBecameInactive:)
                            name:NSWorkspaceSessionDidResignActiveNotification object:nil];

    // This timer only compares two local file modification dates. It never
    // reads websites and never creates a visible task message.
    self.reloadTimer = [NSTimer scheduledTimerWithTimeInterval:10.0
                                                        target:self
                                                      selector:@selector(reloadIfFilesChanged:)
                                                      userInfo:nil
                                                       repeats:YES];
    self.modifierTimer = [NSTimer timerWithTimeInterval:0.05
                                                target:self
                                              selector:@selector(updateLinkModeFromModifierKeys:)
                                              userInfo:nil
                                               repeats:YES];
    [[NSRunLoop mainRunLoop] addTimer:self.modifierTimer forMode:NSRunLoopCommonModes];
}

- (void)applicationWillTerminate:(NSNotification *)notification {
    (void)notification;
    [self.reloadTimer invalidate];
    [self.modifierTimer invalidate];
    [self removeAllPanels];
    [NSNotificationCenter.defaultCenter removeObserver:self];
    [NSWorkspace.sharedWorkspace.notificationCenter removeObserver:self];
}

- (void)updateLinkModeFromModifierKeys:(NSTimer *)timer {
    (void)timer;
    CGEventFlags flags = CGEventSourceFlagsState(kCGEventSourceStateCombinedSessionState);
    BOOL shouldActivate = (flags & kCGEventFlagMaskAlternate) != 0;
    if (shouldActivate == self.linkModeActive) {
        return;
    }
    if (shouldActivate) {
        NSDate *now = [NSDate date];
        if (self.lastOptionPress != nil &&
            [now timeIntervalSinceDate:self.lastOptionPress] <= 0.4) {
            self.lastOptionPress = nil;
            if ([NSFileManager.defaultManager fileExistsAtPath:self.localReportURL.path]) {
                [[NSWorkspace sharedWorkspace] openURL:self.localReportURL];
            }
        } else {
            self.lastOptionPress = now;
        }
    }
    self.linkModeActive = shouldActivate;
    for (NSPanel *panel in self.panels) {
        panel.ignoresMouseEvents = !shouldActivate;
        if ([panel.contentView isKindOfClass:[PlannerHotspotView class]]) {
            ((PlannerHotspotView *)panel.contentView).linkModeActive = shouldActivate;
        }
    }
}

- (void)sessionBecameInactive:(NSNotification *)notification {
    (void)notification;
    if (!self.linkModeActive) {
        return;
    }
    self.linkModeActive = NO;
    self.lastOptionPress = nil;
    for (NSPanel *panel in self.panels) {
        panel.ignoresMouseEvents = YES;
        if ([panel.contentView isKindOfClass:[PlannerHotspotView class]]) {
            ((PlannerHotspotView *)panel.contentView).linkModeActive = NO;
        }
    }
}

- (void)reloadIfFilesChanged:(NSTimer *)timer {
    (void)timer;
    NSDate *manifestDate = [self modificationDateForURL:self.manifestURL];
    NSDate *statusDate = [self modificationDateForURL:self.wallpaperStatusURL];
    if (![self datesEqual:manifestDate other:self.manifestModificationDate] ||
        ![self datesEqual:statusDate other:self.wallpaperStatusModificationDate]) {
        [self reloadHotspots];
    }
}

- (void)displayConfigurationChanged:(NSNotification *)notification { (void)notification; [self reloadHotspots]; }
- (void)desktopContextChanged:(NSNotification *)notification { (void)notification; [self reloadHotspots]; }

- (void)sessionBecameActive:(NSNotification *)notification {
    (void)notification;
    [self reloadHotspots];
    if ([self.lastCatchupRequest timeIntervalSinceNow] > -30.0) {
        return;
    }
    self.lastCatchupRequest = [NSDate date];
    [self launchCatchupDispatcher];
}

- (void)launchCatchupDispatcher {
    if (![NSFileManager.defaultManager fileExistsAtPath:kPythonPath] ||
        ![NSFileManager.defaultManager fileExistsAtPath:self.dispatcherURL.path]) {
        return;
    }
    NSTask *task = [[NSTask alloc] init];
    task.executableURL = [NSURL fileURLWithPath:kPythonPath];
    task.arguments = @[self.dispatcherURL.path];
    task.standardOutput = [NSFileHandle fileHandleWithNullDevice];
    task.standardError = [NSFileHandle fileHandleWithNullDevice];
    @try {
        [task launchAndReturnError:nil];
    } @catch (__unused NSException *exception) {
    }
}

- (BOOL)datesEqual:(NSDate *)first other:(NSDate *)second {
    return (first == nil && second == nil) || [first isEqualToDate:second];
}

- (NSDate *)modificationDateForURL:(NSURL *)URL {
    NSDictionary *attributes = [NSFileManager.defaultManager attributesOfItemAtPath:URL.path error:nil];
    return attributes[NSFileModificationDate];
}

- (nullable NSDictionary *)dictionaryFromURL:(NSURL *)URL {
    NSData *data = [NSData dataWithContentsOfURL:URL options:0 error:nil];
    if (data == nil) {
        return nil;
    }
    id value = [NSJSONSerialization JSONObjectWithData:data options:0 error:nil];
    return [value isKindOfClass:[NSDictionary class]] ? value : nil;
}

- (nullable NSString *)sha256ForFileAtPath:(NSString *)path {
    NSFileHandle *handle = [NSFileHandle fileHandleForReadingAtPath:path];
    if (handle == nil) {
        return nil;
    }
    CC_SHA256_CTX context;
    CC_SHA256_Init(&context);
    while (YES) {
        @autoreleasepool {
            NSData *chunk = [handle readDataOfLength:1024 * 1024];
            if (chunk.length == 0) {
                break;
            }
            CC_SHA256_Update(&context, chunk.bytes, (CC_LONG)chunk.length);
        }
    }
    [handle closeFile];
    unsigned char digest[CC_SHA256_DIGEST_LENGTH];
    CC_SHA256_Final(digest, &context);
    NSMutableString *result = [NSMutableString stringWithCapacity:CC_SHA256_DIGEST_LENGTH * 2];
    for (NSInteger index = 0; index < CC_SHA256_DIGEST_LENGTH; index++) {
        [result appendFormat:@"%02x", digest[index]];
    }
    return result;
}

- (BOOL)isSafeRemoteURLString:(NSString *)rawURL {
    NSURLComponents *components = [NSURLComponents componentsWithString:rawURL];
    NSString *scheme = components.scheme.lowercaseString;
    return ([scheme isEqualToString:@"https"] || [scheme isEqualToString:@"http"]) &&
        components.host.length > 0 && components.user.length == 0 && components.password.length == 0;
}

- (nullable NSDictionary *)validatedManifestWithReason:(NSString **)reason {
    NSDictionary *manifest = [self dictionaryFromURL:self.manifestURL];
    NSDictionary *wallpaper = [self dictionaryFromURL:self.wallpaperStatusURL];
    if (manifest == nil || wallpaper == nil) {
        *reason = @"manifest_or_wallpaper_status_missing";
        return nil;
    }
    NSDictionary *canvas = [manifest[@"canvas"] isKindOfClass:[NSDictionary class]] ? manifest[@"canvas"] : nil;
    NSArray *hotspots = [manifest[@"hotspots"] isKindOfClass:[NSArray class]] ? manifest[@"hotspots"] : nil;
    NSString *manifestHash = [manifest[@"image_sha256"] isKindOfClass:[NSString class]] ? manifest[@"image_sha256"] : nil;
    NSString *wallpaperHash = [wallpaper[@"image_sha256"] isKindOfClass:[NSString class]] ? wallpaper[@"image_sha256"] : nil;
    NSString *target = [wallpaper[@"target"] isKindOfClass:[NSString class]] ? wallpaper[@"target"] : nil;
    if (![manifest[@"schema_version"] isEqual:@1] ||
        ![canvas[@"coordinate_origin"] isEqual:@"top_left"] ||
        [canvas[@"width"] doubleValue] <= 0 || [canvas[@"height"] doubleValue] <= 0 ||
        hotspots == nil || manifestHash.length != 64) {
        *reason = @"invalid_manifest_schema";
        return nil;
    }
    if (![wallpaper[@"status"] isEqual:@"ok"] ||
        ![wallpaper[@"presentation_verified"] boolValue] ||
        ![wallpaper[@"current_desktop_verified"] boolValue] ||
        ![wallpaper[@"all_spaces_verified"] boolValue] ||
        target.length == 0 || ![manifestHash isEqualToString:wallpaperHash]) {
        *reason = @"wallpaper_not_verified_or_hash_mismatch";
        return nil;
    }
    NSString *actualHash = [self sha256ForFileAtPath:target];
    if (![manifestHash isEqualToString:actualHash]) {
        *reason = @"wallpaper_file_hash_mismatch";
        return nil;
    }
    double imageWidth = [canvas[@"width"] doubleValue];
    double imageHeight = [canvas[@"height"] doubleValue];
    NSMutableSet *identifiers = [NSMutableSet set];
    for (id rawItem in hotspots) {
        if (![rawItem isKindOfClass:[NSDictionary class]]) {
            *reason = @"invalid_hotspot_item";
            return nil;
        }
        NSDictionary *item = rawItem;
        NSString *identifier = [item[@"id"] isKindOfClass:[NSString class]] ? item[@"id"] : @"";
        NSDictionary *rect = [item[@"rect"] isKindOfClass:[NSDictionary class]] ? item[@"rect"] : nil;
        double x = [rect[@"x"] doubleValue];
        double y = [rect[@"y"] doubleValue];
        double width = [rect[@"width"] doubleValue];
        double height = [rect[@"height"] doubleValue];
        NSString *remoteURL = [item[@"url"] isKindOfClass:[NSString class]] ? item[@"url"] : nil;
        NSString *action = [item[@"action"] isKindOfClass:[NSString class]] ? item[@"action"] : nil;
        BOOL controlledAction = [action isEqual:@"open_local_report"] ||
            [action isEqual:@"start_intern_application_batch"];
        BOOL targetIsValid = remoteURL.length > 0 ? [self isSafeRemoteURLString:remoteURL] : controlledAction;
        if (identifier.length == 0 || [identifiers containsObject:identifier] || rect == nil ||
            x < 0 || y < 0 || width <= 0 || height <= 0 ||
            x + width > imageWidth || y + height > imageHeight || !targetIsValid) {
            *reason = @"invalid_hotspot_item";
            return nil;
        }
        [identifiers addObject:identifier];
    }
    return manifest;
}

- (void)removeAllPanels {
    for (NSPanel *panel in self.panels) {
        [panel orderOut:nil];
        [panel close];
    }
    [self.panels removeAllObjects];
}

- (NSRect)screenRectForImageRect:(NSDictionary *)rect
                          screen:(NSScreen *)screen
                      imageWidth:(CGFloat)imageWidth
                     imageHeight:(CGFloat)imageHeight {
    NSRect frame = screen.frame;
    CGFloat scale = MAX(frame.size.width / imageWidth, frame.size.height / imageHeight);
    CGFloat renderedWidth = imageWidth * scale;
    CGFloat renderedHeight = imageHeight * scale;
    CGFloat offsetX = (frame.size.width - renderedWidth) / 2.0;
    CGFloat offsetY = (frame.size.height - renderedHeight) / 2.0;
    CGFloat x = [rect[@"x"] doubleValue];
    CGFloat y = [rect[@"y"] doubleValue];
    CGFloat width = [rect[@"width"] doubleValue];
    CGFloat height = [rect[@"height"] doubleValue];
    return NSMakeRect(frame.origin.x + offsetX + x * scale,
                      frame.origin.y + frame.size.height - offsetY - (y + height) * scale,
                      width * scale,
                      height * scale);
}

- (void)reloadHotspots {
    self.manifestModificationDate = [self modificationDateForURL:self.manifestURL];
    self.wallpaperStatusModificationDate = [self modificationDateForURL:self.wallpaperStatusURL];
    NSString *reason = nil;
    NSDictionary *manifest = [self validatedManifestWithReason:&reason];
    [self removeAllPanels];
    if (manifest == nil) {
        [self publishOverlayStatus:@{@"state": @"disabled", @"reason": reason ?: @"validation_failed",
                                     @"hotspot_count": @0, @"panel_count": @0}];
        return;
    }

    NSDictionary *canvas = manifest[@"canvas"];
    CGFloat imageWidth = [canvas[@"width"] doubleValue];
    CGFloat imageHeight = [canvas[@"height"] doubleValue];
    NSArray *hotspots = manifest[@"hotspots"];
    NSDictionary *wallpaper = [self dictionaryFromURL:self.wallpaperStatusURL];
    NSString *wallpaperPath = [wallpaper[@"target"] isKindOfClass:[NSString class]] ? wallpaper[@"target"] : @"";
    NSImage *wallpaperImage = [[NSImage alloc] initWithContentsOfFile:wallpaperPath];
    wallpaperImage.size = NSMakeSize(imageWidth, imageHeight);
    NSInteger desktopIconLevel = CGWindowLevelForKey(kCGDesktopIconWindowLevelKey);
    for (NSScreen *screen in NSScreen.screens) {
        for (NSDictionary *item in hotspots) {
            NSRect frame = [self screenRectForImageRect:item[@"rect"] screen:screen
                                             imageWidth:imageWidth imageHeight:imageHeight];
            PlannerHotspotPanel *panel = [[PlannerHotspotPanel alloc]
                initWithContentRect:frame
                         styleMask:NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel
                           backing:NSBackingStoreBuffered defer:NO screen:screen];
            panel.opaque = NO;
            panel.backgroundColor = NSColor.clearColor;
            panel.hasShadow = NO;
            panel.hidesOnDeactivate = NO;
            // Normal desktop interaction always wins. Holding Option activates
            // the aligned link cards and temporarily raises their pixels above
            // any Finder icons occupying the same area.
            panel.ignoresMouseEvents = !self.linkModeActive;
            panel.releasedWhenClosed = NO;
            panel.level = desktopIconLevel + 1;
            panel.collectionBehavior = NSWindowCollectionBehaviorCanJoinAllSpaces |
                NSWindowCollectionBehaviorStationary | NSWindowCollectionBehaviorIgnoresCycle |
                NSWindowCollectionBehaviorFullScreenAuxiliary;
            panel.contentView = [[PlannerHotspotView alloc]
                initWithFrame:NSMakeRect(0, 0, frame.size.width, frame.size.height)
                         item:item
               localReportURL:self.localReportURL
               wallpaperImage:wallpaperImage
             wallpaperCanvas:NSMakeSize(imageWidth, imageHeight)];
            ((PlannerHotspotView *)panel.contentView).linkModeActive = self.linkModeActive;
            [panel orderFrontRegardless];
            [self.panels addObject:panel];
        }
    }
    [self publishOverlayStatus:@{
        @"state": @"active",
        @"reason": @"wallpaper_and_manifest_verified",
        @"hotspot_count": @(hotspots.count),
        @"panel_count": @(self.panels.count),
        @"screen_count": @(NSScreen.screens.count),
        @"image_sha256": manifest[@"image_sha256"] ?: @"",
        @"generated_at": manifest[@"generated_at"] ?: @"",
    }];
}

- (void)publishOverlayStatus:(NSDictionary *)status {
    NSMutableDictionary *payload = [status mutableCopy];
    NSISO8601DateFormatter *formatter = [[NSISO8601DateFormatter alloc] init];
    payload[@"checked_at"] = [formatter stringFromDate:[NSDate date]];
    payload[@"interaction_model"] = @"wallpaper_aligned_option_hold_hotspots";
    payload[@"activation_modifier"] = @"option";
    payload[@"foreground_shortcut"] = @"double_tap_option";
    payload[@"default_click_through"] = @YES;
    payload[@"card_reveal_in_link_mode"] = @YES;
    payload[@"separate_planner_window"] = @NO;
    self.lastOverlayStatus = payload;
    NSData *data = [NSJSONSerialization dataWithJSONObject:payload options:NSJSONWritingPrettyPrinted error:nil];
    if (data != nil) {
        [data writeToURL:self.overlayStatusURL options:NSDataWritingAtomic error:nil];
    }
}

@end


int main(int argc, const char *argv[]) {
    @autoreleasepool {
        BOOL validateOnly = NO;
        for (int index = 1; index < argc; index++) {
            if (strcmp(argv[index], "--validate-manifest") == 0) {
                validateOnly = YES;
            }
        }
        NSApplication *application = NSApplication.sharedApplication;
        PlannerAppDelegate *delegate = [[PlannerAppDelegate alloc] init];
        delegate.validateOnly = validateOnly;
        application.delegate = delegate;
        [application setActivationPolicy:NSApplicationActivationPolicyAccessory];
        [application run];
    }
    return 0;
}
