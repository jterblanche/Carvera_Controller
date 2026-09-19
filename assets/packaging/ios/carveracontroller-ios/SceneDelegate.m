#import "SceneDelegate.h"
#import <objc/runtime.h>

static UIWindowScene *CarveraActiveWindowScene = nil;

static void CarveraAttachWindowToActiveScene(UIWindow *window)
{
    if (!window || !CarveraActiveWindowScene) {
        return;
    }
    if (window.windowScene != CarveraActiveWindowScene) {
        window.windowScene = CarveraActiveWindowScene;
    }
}

static void CarveraAttachExistingWindows(void)
{
    if (!CarveraActiveWindowScene) {
        return;
    }
    for (UIWindow *window in CarveraActiveWindowScene.windows) {
        CarveraAttachWindowToActiveScene(window);
    }
    for (UIWindow *window in [UIApplication sharedApplication].windows) {
        CarveraAttachWindowToActiveScene(window);
    }
}

@implementation UIWindow (CarveraSceneAttachment)

+ (void)load
{
    static dispatch_once_t onceToken;
    dispatch_once(&onceToken, ^{
        Method original = class_getInstanceMethod(self, @selector(initWithFrame:));
        Method swizzled = class_getInstanceMethod(self, @selector(carvera_initWithFrame:));
        if (original && swizzled) {
            method_exchangeImplementations(original, swizzled);
        }
    });
}

- (instancetype)carvera_initWithFrame:(CGRect)frame
{
    UIWindow *window = [self carvera_initWithFrame:frame];
    CarveraAttachWindowToActiveScene(window);
    return window;
}

@end

@implementation SceneDelegate

+ (UIWindowScene *)activeWindowScene
{
    return CarveraActiveWindowScene;
}

+ (UIWindow *)keyWindow
{
    UIWindowScene *scene = CarveraActiveWindowScene;
    if (scene) {
        for (UIWindow *window in scene.windows) {
            if (window.isKeyWindow) {
                return window;
            }
        }
        if (scene.windows.count > 0) {
            return scene.windows.lastObject;
        }
    }
    for (UIScene *connected in [UIApplication sharedApplication].connectedScenes) {
        if (![connected isKindOfClass:[UIWindowScene class]]) {
            continue;
        }
        UIWindowScene *windowScene = (UIWindowScene *)connected;
        for (UIWindow *window in windowScene.windows) {
            if (window.isKeyWindow) {
                return window;
            }
        }
    }
    return [UIApplication sharedApplication].keyWindow;
}

- (void)dealloc
{
    [_window release];
    [super dealloc];
}

- (void)scene:(UIScene *)scene
willConnectToSession:(UISceneSession *)session
      options:(UISceneConnectionOptions *)connectionOptions
{
    if (![scene isKindOfClass:[UIWindowScene class]]) {
        return;
    }
    UIWindowScene *windowScene = (UIWindowScene *)scene;
    [CarveraActiveWindowScene release];
    CarveraActiveWindowScene = [windowScene retain];
    CarveraAttachExistingWindows();
}

- (void)sceneDidDisconnect:(UIScene *)scene
{
    if (scene == CarveraActiveWindowScene) {
        [CarveraActiveWindowScene release];
        CarveraActiveWindowScene = nil;
    }
}

@end

// SDL2's app delegate does not implement scene configuration. Adding it here
// covers restored sessions; Info.plist still has to declare the scene so iOS 27
// will launch the app at all.
@interface SDLUIKitDelegate : NSObject
@end

@implementation SDLUIKitDelegate (CarveraSceneLifecycle)

- (UISceneConfiguration *)application:(UIApplication *)application
configurationForConnectingSceneSession:(UISceneSession *)connectingSceneSession
                              options:(UISceneConnectionOptions *)options
{
    UISceneConfiguration *config =
        [[[UISceneConfiguration alloc] initWithName:@"Default Configuration"
                                        sessionRole:connectingSceneSession.role] autorelease];
    config.delegateClass = [SceneDelegate class];
    return config;
}

@end
