/*
 * JSON 参数取值辅助
 *
 * 为什么要单独做一层：cJSON 把所有数字都存成 double，直接取再转型
 * 有两个常见坑 ——
 *   1) 字段不存在（模型没传可选参数）时 cJSON_GetObjectItem 返回 NULL，
 *      直接解引用会崩；
 *   2) 字段存在但类型不对（模型把数字传成字符串 "0.3"）时，
 *      静默取到 0 会让机器人以错误速度运动。
 *
 * 这层的约定是：**字段缺失用默认值，类型不对标记出来让调用方决定**
 * （通常回 bad_params，而不是带着错值继续执行）。
 * 对机器人这种会真的动起来的设备，"安静地用错值"是最糟的选择。
 */

#pragma once

#include <stdbool.h>

#include "cJSON.h"

#ifdef __cplusplus
extern "C" {
#endif

/* 字段是否存在 */
bool bot_param_has(const cJSON *obj, const char *key);

/*
 * 取整数。字段缺失返回 def；
 * 存在但不是数字则返回 def 并把 *type_error 置 true。
 * type_error 可传 NULL。
 */
int bot_param_int(const cJSON *obj, const char *key, int def, bool *type_error);

/* 取浮点。也接受字符串形式的数字（模型常这么干），例如 "0.3"。 */
float bot_param_float(const cJSON *obj, const char *key, float def, bool *type_error);

/* 取布尔。兼容 JSON true/false 与数字 0/1，以及字符串 "true"/"1"。 */
bool bot_param_bool(const cJSON *obj, const char *key, bool def, bool *type_error);

/*
 * 取字符串。
 * 返回的是 cJSON 内部指针，**不要 free，也不要跨帧持有**。
 * 字段缺失或类型不对时返回 def。
 */
const char *bot_param_str(const cJSON *obj, const char *key, const char *def);

#ifdef __cplusplus
}
#endif
