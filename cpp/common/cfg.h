// cfg.h — tiny header-only config: key=value file parser + `--key=value`
// argv overrides. No getopt, no external deps.
//
// File format: blank lines and lines starting with '#' (after leading
// whitespace) are ignored; otherwise `key=value`, whitespace around key and
// value is trimmed. Unknown keys are fine — this is just a string map, not
// a schema.
#ifndef JNX_CFG_H
#define JNX_CFG_H

#include <cctype>
#include <cstdlib>
#include <fstream>
#include <sstream>
#include <string>
#include <unordered_map>

namespace jnx {

inline std::string cfg_trim(const std::string& s) {
    size_t start = 0;
    while (start < s.size() &&
           std::isspace(static_cast<unsigned char>(s[start]))) {
        ++start;
    }
    size_t end = s.size();
    while (end > start &&
           std::isspace(static_cast<unsigned char>(s[end - 1]))) {
        --end;
    }
    return s.substr(start, end - start);
}

class Cfg {
public:
    // Parses a key=value file. Returns false if the file could not be
    // opened; a missing file is not fatal for callers that treat defaults
    // as sufficient. Malformed lines (no '=') are skipped.
    bool load_file(const std::string& path) {
        std::ifstream in(path.c_str());
        if (!in.is_open()) {
            return false;
        }
        std::string line;
        while (std::getline(in, line)) {
            std::string trimmed = cfg_trim(line);
            if (trimmed.empty() || trimmed[0] == '#') {
                continue;
            }
            size_t eq = trimmed.find('=');
            if (eq == std::string::npos) {
                continue;
            }
            std::string key = cfg_trim(trimmed.substr(0, eq));
            std::string value = cfg_trim(trimmed.substr(eq + 1));
            if (!key.empty()) {
                values_[key] = value;
            }
        }
        return true;
    }

    // Applies `--key=value` overrides from argv (argv[0] is the program
    // name and is skipped). Args not matching `--key=value` are ignored.
    void apply_args(int argc, char** argv) {
        for (int i = 1; i < argc; ++i) {
            std::string arg(argv[i]);
            if (arg.size() < 3 || arg[0] != '-' || arg[1] != '-') {
                continue;
            }
            std::string rest = arg.substr(2);
            size_t eq = rest.find('=');
            if (eq == std::string::npos) {
                continue;
            }
            std::string key = rest.substr(0, eq);
            std::string value = rest.substr(eq + 1);
            if (!key.empty()) {
                values_[key] = value;
            }
        }
    }

    std::string get(const std::string& key, const std::string& def) const {
        std::unordered_map<std::string, std::string>::const_iterator it =
            values_.find(key);
        if (it == values_.end()) {
            return def;
        }
        return it->second;
    }

    long get_int(const std::string& key, long def) const {
        std::unordered_map<std::string, std::string>::const_iterator it =
            values_.find(key);
        if (it == values_.end() || it->second.empty()) {
            return def;
        }
        char* endptr = NULL;
        long v = std::strtol(it->second.c_str(), &endptr, 10);
        if (endptr == it->second.c_str()) {
            return def;
        }
        return v;
    }

private:
    std::unordered_map<std::string, std::string> values_;
};

} // namespace jnx

#endif // JNX_CFG_H
