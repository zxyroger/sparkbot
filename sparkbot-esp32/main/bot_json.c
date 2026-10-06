#include "bot_json.h"

#include <stdlib.h>
#include <string.h>

bool bot_param_has(const cJSON *obj, const char *key)
{
    if (obj == NULL || key == NULL) {
        return false;
    }
    return cJSON_GetObjectItemCaseSensitive(obj, key) != NULL;
}

int bot_param_int(const cJSON *obj, const char *key, int def, bool *type_error)
{
    if (type_error != NULL) {
        *type_error = false;
    }
    if (obj == NULL || key == NULL) {
        return def;
    }

    const cJSON *item = cJSON_GetObjectItemCaseSensitive(obj, key);
    if (item == NULL || cJSON_IsNull(item)) {
        return def;
    }
    if (cJSON_IsNumber(item)) {
        return (int)item->valuedouble;
    }
    if (cJSON_IsBool(item)) {
        return cJSON_IsTrue(item) ? 1 : 0;
    }
    /* 模型有时把数字写成字符串 */
    if (cJSON_IsString(item) && item->valuestring != NULL) {
        char *end = NULL;
        long v = strtol(item->valuestring, &end, 10);
        if (end != NULL && *end == '\0') {
            return (int)v;
        }
    }

    if (type_error != NULL) {
        *type_error = true;
    }
    return def;
}

float bot_param_float(const cJSON *obj, const char *key, float def, bool *type_error)
{
    if (type_error != NULL) {
        *type_error = false;
    }
    if (obj == NULL || key == NULL) {
        return def;
    }

    const cJSON *item = cJSON_GetObjectItemCaseSensitive(obj, key);
    if (item == NULL || cJSON_IsNull(item)) {
        return def;
    }
    if (cJSON_IsNumber(item)) {
        return (float)item->valuedouble;
    }
    if (cJSON_IsString(item) && item->valuestring != NULL) {
        char *end = NULL;
        float v = strtof(item->valuestring, &end);
        if (end != NULL && *end == '\0') {
            return v;
        }
    }

    if (type_error != NULL) {
        *type_error = true;
    }
    return def;
}

bool bot_param_bool(const cJSON *obj, const char *key, bool def, bool *type_error)
{
    if (type_error != NULL) {
        *type_error = false;
    }
    if (obj == NULL || key == NULL) {
        return def;
    }

    const cJSON *item = cJSON_GetObjectItemCaseSensitive(obj, key);
    if (item == NULL || cJSON_IsNull(item)) {
        return def;
    }
    if (cJSON_IsBool(item)) {
        return cJSON_IsTrue(item);
    }
    if (cJSON_IsNumber(item)) {
        return item->valuedouble != 0.0;
    }
    if (cJSON_IsString(item) && item->valuestring != NULL) {
        const char *s = item->valuestring;
        if (strcasecmp(s, "true") == 0 || strcmp(s, "1") == 0 || strcasecmp(s, "yes") == 0) {
            return true;
        }
        if (strcasecmp(s, "false") == 0 || strcmp(s, "0") == 0 || strcasecmp(s, "no") == 0) {
            return false;
        }
    }

    if (type_error != NULL) {
        *type_error = true;
    }
    return def;
}

const char *bot_param_str(const cJSON *obj, const char *key, const char *def)
{
    if (obj == NULL || key == NULL) {
        return def;
    }
    const cJSON *item = cJSON_GetObjectItemCaseSensitive(obj, key);
    if (item == NULL || !cJSON_IsString(item) || item->valuestring == NULL) {
        return def;
    }
    return item->valuestring;
}
