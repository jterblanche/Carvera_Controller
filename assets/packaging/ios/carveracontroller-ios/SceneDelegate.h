#import <UIKit/UIKit.h>

@interface SceneDelegate : UIResponder <UIWindowSceneDelegate>
@property (retain, nonatomic) UIWindow *window;
+ (UIWindowScene *)activeWindowScene;
+ (UIWindow *)keyWindow;
@end
