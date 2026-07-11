// jsonvec.h — minimal JSON reader for EXACTLY the golden-vector file shape:
//
//   {"vectors": [{"name": "...", "type": "...", "hex": "...",
//                 "fields": {"k": <int-or-string>, ...}}, ...]}
//
// String values may contain only plain characters (no escapes are used in
// the vector files; an escape fails loudly). Integers are non-negative and
// fit uint64. Anything unexpected -> loud failure (message + false).
// NOT a general JSON parser and never will be. Test-only helper.
#ifndef JNX_JSONVEC_H
#define JNX_JSONVEC_H

#include <cstdint>
#include <cstdio>
#include <fstream>
#include <map>
#include <sstream>
#include <string>
#include <vector>

namespace jsonvec {

struct Vector {
    std::string name;
    std::string type;
    std::string hex;
    std::map<std::string, std::string> str_fields;
    std::map<std::string, uint64_t> int_fields;

    bool has_int(const std::string& k) const {
        return int_fields.find(k) != int_fields.end();
    }
    bool has_str(const std::string& k) const {
        return str_fields.find(k) != str_fields.end();
    }
};

class Parser {
public:
    Parser(const std::string& text) : s_(text), pos_(0) {}

    bool fail(const char* why) {
        std::fprintf(stderr, "jsonvec: parse error at offset %zu: %s\n", pos_,
                     why);
        return false;
    }

    void skip_ws() {
        while (pos_ < s_.size() &&
               (s_[pos_] == ' ' || s_[pos_] == '\t' || s_[pos_] == '\n' ||
                s_[pos_] == '\r')) {
            ++pos_;
        }
    }

    bool expect(char c) {
        skip_ws();
        if (pos_ >= s_.size() || s_[pos_] != c) {
            std::fprintf(stderr,
                         "jsonvec: parse error at offset %zu: expected '%c'\n",
                         pos_, c);
            return false;
        }
        ++pos_;
        return true;
    }

    bool peek(char c) {
        skip_ws();
        return pos_ < s_.size() && s_[pos_] == c;
    }

    bool parse_string(std::string& out) {
        if (!expect('"')) return false;
        out.clear();
        while (pos_ < s_.size() && s_[pos_] != '"') {
            if (s_[pos_] == '\\') {
                return fail("string escapes not supported");
            }
            out.push_back(s_[pos_]);
            ++pos_;
        }
        if (pos_ >= s_.size()) return fail("unterminated string");
        ++pos_; // closing quote
        return true;
    }

    bool parse_uint(uint64_t& out) {
        skip_ws();
        if (pos_ >= s_.size() || s_[pos_] < '0' || s_[pos_] > '9') {
            return fail("expected non-negative integer");
        }
        out = 0;
        while (pos_ < s_.size() && s_[pos_] >= '0' && s_[pos_] <= '9') {
            out = out * 10 + static_cast<uint64_t>(s_[pos_] - '0');
            ++pos_;
        }
        return true;
    }

    // Parses the "fields" object: string or uint values only.
    bool parse_fields(Vector& v) {
        if (!expect('{')) return false;
        if (peek('}')) {
            ++pos_;
            return true;
        }
        for (;;) {
            std::string key;
            skip_ws();
            if (!parse_string(key)) return false;
            if (!expect(':')) return false;
            skip_ws();
            if (peek('"')) {
                std::string sval;
                if (!parse_string(sval)) return false;
                v.str_fields[key] = sval;
            } else {
                uint64_t ival = 0;
                if (!parse_uint(ival)) return false;
                v.int_fields[key] = ival;
            }
            skip_ws();
            if (peek(',')) {
                ++pos_;
                continue;
            }
            return expect('}');
        }
    }

    bool parse_vector(Vector& v) {
        if (!expect('{')) return false;
        for (;;) {
            std::string key;
            skip_ws();
            if (!parse_string(key)) return false;
            if (!expect(':')) return false;
            skip_ws();
            if (key == "name") {
                if (!parse_string(v.name)) return false;
            } else if (key == "type") {
                if (!parse_string(v.type)) return false;
            } else if (key == "hex") {
                if (!parse_string(v.hex)) return false;
            } else if (key == "fields") {
                if (!parse_fields(v)) return false;
            } else {
                return fail("unexpected key in vector object");
            }
            skip_ws();
            if (peek(',')) {
                ++pos_;
                continue;
            }
            return expect('}');
        }
    }

    bool parse_file(std::vector<Vector>& out) {
        if (!expect('{')) return false;
        std::string key;
        if (!parse_string(key)) return false;
        if (key != "vectors") return fail("expected top-level key 'vectors'");
        if (!expect(':')) return false;
        if (!expect('[')) return false;
        if (peek(']')) {
            ++pos_;
        } else {
            for (;;) {
                Vector v;
                if (!parse_vector(v)) return false;
                out.push_back(v);
                skip_ws();
                if (peek(',')) {
                    ++pos_;
                    continue;
                }
                if (!expect(']')) return false;
                break;
            }
        }
        if (!expect('}')) return false;
        skip_ws();
        if (pos_ != s_.size()) return fail("trailing garbage after document");
        return true;
    }

private:
    const std::string& s_;
    size_t pos_;
};

// Loads a vector file; returns false (after printing why) on any problem.
inline bool load(const std::string& path, std::vector<Vector>& out) {
    std::ifstream in(path.c_str());
    if (!in.is_open()) {
        std::fprintf(stderr, "jsonvec: cannot open %s\n", path.c_str());
        return false;
    }
    std::ostringstream ss;
    ss << in.rdbuf();
    std::string text = ss.str();
    Parser p(text);
    return p.parse_file(out);
}

// Hex string -> bytes. Returns false on odd length / non-hex chars.
inline bool hex_to_bytes(const std::string& hex,
                         std::vector<unsigned char>& out) {
    if (hex.size() % 2 != 0) {
        std::fprintf(stderr, "jsonvec: odd-length hex string\n");
        return false;
    }
    out.clear();
    out.reserve(hex.size() / 2);
    for (size_t i = 0; i < hex.size(); i += 2) {
        unsigned v = 0;
        for (size_t j = 0; j < 2; ++j) {
            char c = hex[i + j];
            v <<= 4;
            if (c >= '0' && c <= '9') {
                v |= static_cast<unsigned>(c - '0');
            } else if (c >= 'a' && c <= 'f') {
                v |= static_cast<unsigned>(c - 'a' + 10);
            } else if (c >= 'A' && c <= 'F') {
                v |= static_cast<unsigned>(c - 'A' + 10);
            } else {
                std::fprintf(stderr, "jsonvec: bad hex char '%c'\n", c);
                return false;
            }
        }
        out.push_back(static_cast<unsigned char>(v));
    }
    return true;
}

} // namespace jsonvec

#endif // JNX_JSONVEC_H
