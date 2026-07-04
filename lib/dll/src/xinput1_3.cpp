#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>
#include <d3d11.h>

#include <algorithm>
#include <cctype>
#include <cstdint>
#include <cwctype>
#include <cstdio>
#include <filesystem>
#include <fstream>
#include <iterator>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <vector>

#pragma comment(lib, "d3d11.lib")

#if defined(_WIN64)
#error This DLL must be built as Win32/x86 for t6sp.exe.
#endif

namespace
{
    struct LocalizeRecord
    {
        std::string name;
        std::string original;
        std::string translated;
    };

    struct StringTableRecord
    {
        std::string tableName;
        uint32_t row = 0;
        uint32_t column = 0;
        std::string original;
        std::string translated;
    };

    struct Glyph32
    {
        uint16_t letter = 0;
        int8_t x0 = 0;
        int8_t y0 = 0;
        int8_t dx = 0;
        int8_t pixelWidth = 0;
        int8_t pixelHeight = 0;
        uint8_t padding = 0;
        float s0 = 0.0f;
        float t0 = 0.0f;
        float s1 = 0.0f;
        float t1 = 0.0f;
    };

    struct FontOverride
    {
        std::string name;
        std::string atlasKey = "gamefonts_pc_720";
        int pixelHeight = 0;
        int isScalingAllowed = 0;
        std::vector<Glyph32> glyphs;
    };

    struct FontAtlasOverride
    {
        std::string key;
        std::vector<uint8_t> rgba;
        uint32_t width = 0;
        uint32_t height = 0;
        ID3D11ShaderResourceView* view = nullptr;
    };

    struct Payload
    {
        std::vector<LocalizeRecord> localize;
        std::vector<StringTableRecord> stringTables;
        std::unordered_map<std::string, std::vector<size_t>> localizeByName;
        std::unordered_map<std::string, std::vector<size_t>> stringTablesByName;
    };

    struct Region
    {
        uintptr_t begin = 0;
        uintptr_t end = 0;
        bool writable = false;
    };

    struct StringTableCell32
    {
        uint32_t stringPtr;
        int32_t hash;
    };

    struct StringTable32
    {
        uint32_t name;
        int32_t columnCount;
        int32_t rowCount;
        uint32_t values;
        uint32_t cellIndex;
    };

    struct ReturnStack
    {
        uintptr_t values[64] = {};
        int depth = 0;
    };

    HMODULE g_module = nullptr;
    std::filesystem::path g_logPath;
    Payload g_payload;
    bool g_payloadLoaded = false;
    bool g_textPayloadLoaded = false;
    bool g_fontPayloadLoaded = false;
    bool g_fileLoggingEnabled = true;
    std::vector<FontOverride> g_fontOverrides;
    std::vector<FontAtlasOverride> g_fontAtlases;
    int g_scanPass = 0;
    DWORD g_returnStackTls = TLS_OUT_OF_INDEXES;
    uintptr_t g_dbFindContinue = 0;
    bool g_dbFindHookInstalled = false;
    volatile LONG g_lookupCalls = 0;
    volatile LONG g_lookupLocalizeCalls = 0;
    volatile LONG g_hookLocalizeCandidates = 0;
    volatile LONG g_hookLocalizePatched = 0;
    volatile LONG g_hookStringTablePatched = 0;
    volatile LONG g_hookFontPatched = 0;
    bool g_startLogged = false;

    constexpr uintptr_t T6_IMAGE_BASE = 0x00400000;
    constexpr uintptr_t T6_POOL_POINTERS_VA = 0x00BD46B8;
    constexpr uintptr_t T6_POOL_CAPACITIES_VA = 0x00BD42F8;
    constexpr uintptr_t T6_XASSET_ENTRIES_VA = 0x01178C50;
    constexpr uintptr_t T6_DB_FIND_XASSET_ENTRY_VA = 0x00778730;
    constexpr uint32_t T6_XASSET_ENTRY_COUNT = 0x752F;
    constexpr uint32_t T6_XASSET_ENTRY_SIZE = 0x10;
    constexpr int T6_ASSET_LOCALIZE = 24;
    constexpr int T6_ASSET_FONT = 20;
    constexpr int T6_ASSET_STRINGTABLE = 42;
    constexpr uint8_t DB_FIND_PROLOGUE[5] = {0x53, 0x8B, 0x5C, 0x24, 0x08};

    std::string NormalizeKey(std::string value)
    {
        std::transform(value.begin(),
                       value.end(),
                       value.begin(),
                       [](unsigned char c)
                       {
                           if (c >= 'a' && c <= 'z')
                               return static_cast<char>(c - ('a' - 'A'));
                           return static_cast<char>(c);
                       });
        return value;
    }

    bool IsReadableProtect(DWORD protect)
    {
        if (protect & PAGE_GUARD)
            return false;
        if (protect == PAGE_NOACCESS)
            return false;

        const DWORD baseProtect = protect & 0xff;
        return baseProtect == PAGE_READONLY || baseProtect == PAGE_READWRITE || baseProtect == PAGE_WRITECOPY || baseProtect == PAGE_EXECUTE_READ
            || baseProtect == PAGE_EXECUTE_READWRITE || baseProtect == PAGE_EXECUTE_WRITECOPY;
    }

    bool IsWritableProtect(DWORD protect)
    {
        if (protect & PAGE_GUARD)
            return false;

        const DWORD baseProtect = protect & 0xff;
        return baseProtect == PAGE_READWRITE || baseProtect == PAGE_WRITECOPY || baseProtect == PAGE_EXECUTE_READWRITE
            || baseProtect == PAGE_EXECUTE_WRITECOPY;
    }

    void Log(const std::string& line)
    {
        OutputDebugStringA(("[T6TR] " + line + "\n").c_str());
        if (g_fileLoggingEnabled && !g_logPath.empty())
        {
            std::ofstream log(g_logPath, std::ios::app | std::ios::binary);
            log << line << "\n";
        }
    }

    std::wstring GetEnvW(const wchar_t* name)
    {
        wchar_t buffer[32768] = {};
        const DWORD count = GetEnvironmentVariableW(name, buffer, static_cast<DWORD>(std::size(buffer)));
        if (!count || count >= std::size(buffer))
            return {};
        return std::wstring(buffer, count);
    }

    std::filesystem::path ModuleDirectory()
    {
        wchar_t path[MAX_PATH] = {};
        GetModuleFileNameW(g_module, path, static_cast<DWORD>(std::size(path)));
        return std::filesystem::path(path).parent_path();
    }

    bool IsT6SpProcess()
    {
        wchar_t path[MAX_PATH] = {};
        GetModuleFileNameW(nullptr, path, static_cast<DWORD>(std::size(path)));
        auto exe = std::filesystem::path(path).filename().wstring();
        std::transform(exe.begin(), exe.end(), exe.begin(), [](wchar_t c) { return static_cast<wchar_t>(towlower(c)); });
        return exe == L"t6sp.exe";
    }

    uintptr_t RebaseT6Va(uintptr_t va)
    {
        const auto moduleBase = reinterpret_cast<uintptr_t>(GetModuleHandleW(nullptr));
        return moduleBase + (va - T6_IMAGE_BASE);
    }

    std::vector<uint8_t> ReadAllBytes(const std::filesystem::path& path)
    {
        std::ifstream file(path, std::ios::binary);
        if (!file)
            return {};
        file.seekg(0, std::ios::end);
        const auto size = file.tellg();
        if (size <= 0)
            return {};
        file.seekg(0, std::ios::beg);
        std::vector<uint8_t> data(static_cast<size_t>(size));
        file.read(reinterpret_cast<char*>(data.data()), static_cast<std::streamsize>(data.size()));
        if (!file)
            return {};
        return data;
    }

    bool ReadU16(const std::vector<uint8_t>& data, size_t& offset, uint16_t& value)
    {
        if (offset + sizeof(value) > data.size())
            return false;
        value = static_cast<uint16_t>(data[offset] | (data[offset + 1] << 8));
        offset += sizeof(value);
        return true;
    }

    bool ReadU32(const std::vector<uint8_t>& data, size_t& offset, uint32_t& value)
    {
        if (offset + sizeof(value) > data.size())
            return false;
        value = static_cast<uint32_t>(data[offset]) | (static_cast<uint32_t>(data[offset + 1]) << 8) | (static_cast<uint32_t>(data[offset + 2]) << 16)
            | (static_cast<uint32_t>(data[offset + 3]) << 24);
        offset += sizeof(value);
        return true;
    }

    bool ReadU64(const std::vector<uint8_t>& data, size_t& offset, uint64_t& value)
    {
        if (offset + sizeof(value) > data.size())
            return false;
        memcpy(&value, data.data() + offset, sizeof(value));
        offset += sizeof(value);
        return true;
    }

    bool ReadString16(const std::vector<uint8_t>& data, size_t& offset, std::string& value)
    {
        uint16_t length = 0;
        if (!ReadU16(data, offset, length) || offset + length > data.size())
            return false;
        value.assign(reinterpret_cast<const char*>(data.data() + offset), length);
        offset += length;
        return true;
    }

    bool ReadString32(const std::vector<uint8_t>& data, size_t& offset, std::string& value)
    {
        uint32_t length = 0;
        if (!ReadU32(data, offset, length) || offset + length > data.size())
            return false;
        value.assign(reinterpret_cast<const char*>(data.data() + offset), length);
        offset += length;
        return true;
    }

    size_t FindMagic(const std::vector<uint8_t>& data, const char* magic)
    {
        if (data.size() < 8)
            return std::string::npos;
        for (size_t i = 0; i + 8 <= data.size(); ++i)
        {
            if (memcmp(data.data() + i, magic, 8) == 0)
                return i;
        }
        return std::string::npos;
    }

    void ParseRuntimeConfig(const std::vector<uint8_t>& data)
    {
        const size_t configOffset = FindMagic(data, "T6TRCFG1");
        if (configOffset == std::string::npos || configOffset + 12 > data.size())
            return;
        size_t offset = configOffset + 8;
        uint32_t flags = 0;
        if (ReadU32(data, offset, flags))
            g_fileLoggingEnabled = (flags & 1u) != 0;
    }

    bool ParseFontPayload(const std::vector<uint8_t>& data, const std::filesystem::path& path)
    {
        const size_t fontOffset = FindMagic(data, "T6TRFNT1");
        if (fontOffset == std::string::npos)
            return false;

        size_t offset = fontOffset + 8;
        uint32_t version = 0;
        uint64_t manifestLength = 0;
        uint32_t fileCount = 0;
        if (!ReadU32(data, offset, version) || !ReadU64(data, offset, manifestLength) || manifestLength > data.size() - offset)
            return false;
        offset += static_cast<size_t>(manifestLength);
        if (!ReadU32(data, offset, fileCount) || fileCount > 128)
            return false;

        std::vector<FontOverride> overrides;
        std::vector<FontAtlasOverride> atlases;
        for (uint32_t fileIndex = 0; fileIndex < fileCount; ++fileIndex)
        {
            uint16_t pathLength = 0;
            uint64_t fileLength = 0;
            if (!ReadU16(data, offset, pathLength) || !ReadU64(data, offset, fileLength)
                || pathLength > data.size() - offset || fileLength > data.size() - offset - pathLength)
            {
                return false;
            }
            const std::string filePath(reinterpret_cast<const char*>(data.data() + offset), pathLength);
            offset += pathLength;
            const size_t fileEnd = offset + static_cast<size_t>(fileLength);

            if (fileLength >= 16 && memcmp(data.data() + offset, "T6RGBA1\0", 8) == 0)
            {
                FontAtlasOverride atlas;
                atlas.key = "gamefonts_pc_720";
                size_t imageOffset = offset + 8;
                if (!ReadU32(data, imageOffset, atlas.width) || !ReadU32(data, imageOffset, atlas.height))
                    return false;
                const uint64_t required = static_cast<uint64_t>(atlas.width) * atlas.height * 4;
                if (!atlas.width || !atlas.height || required != fileEnd - imageOffset)
                    return false;
                atlas.rgba.assign(data.begin() + imageOffset, data.begin() + fileEnd);
                atlases.push_back(std::move(atlas));
            }
            else if (fileLength >= 18 && memcmp(data.data() + offset, "T6RGBA2\0", 8) == 0)
            {
                FontAtlasOverride atlas;
                size_t imageOffset = offset + 8;
                if (!ReadString16(data, imageOffset, atlas.key)
                    || !ReadU32(data, imageOffset, atlas.width) || !ReadU32(data, imageOffset, atlas.height))
                    return false;
                const uint64_t required = static_cast<uint64_t>(atlas.width) * atlas.height * 4;
                if (atlas.key.empty() || !atlas.width || !atlas.height || required != fileEnd - imageOffset)
                    return false;
                atlas.rgba.assign(data.begin() + imageOffset, data.begin() + fileEnd);
                atlases.push_back(std::move(atlas));
            }
            else if (fileLength >= 8 && (memcmp(data.data() + offset, "T6FMETR1", 8) == 0
                || memcmp(data.data() + offset, "T6FMETR2", 8) == 0))
            {
                const bool version2 = memcmp(data.data() + offset, "T6FMETR2", 8) == 0;
                size_t metricOffset = offset + 8;
                FontOverride font;
                uint32_t glyphCount = 0;
                uint32_t pixelHeight = 0;
                uint32_t isScalingAllowed = 0;
                if (!ReadString16(data, metricOffset, font.name)
                    || (version2 && !ReadString16(data, metricOffset, font.atlasKey))
                    || !ReadU32(data, metricOffset, pixelHeight)
                    || !ReadU32(data, metricOffset, isScalingAllowed)
                    || !ReadU32(data, metricOffset, glyphCount) || glyphCount > 4096)
                {
                    return false;
                }
                font.pixelHeight = static_cast<int>(pixelHeight);
                font.isScalingAllowed = static_cast<int>(isScalingAllowed);
                font.glyphs.reserve(glyphCount);
                for (uint32_t glyphIndex = 0; glyphIndex < glyphCount; ++glyphIndex)
                {
                    if (metricOffset + 23 > fileEnd)
                        return false;
                    Glyph32 glyph;
                    memcpy(&glyph.letter, data.data() + metricOffset, 2);
                    memcpy(&glyph.x0, data.data() + metricOffset + 2, 5);
                    memcpy(&glyph.s0, data.data() + metricOffset + 7, 16);
                    metricOffset += 23;
                    font.glyphs.push_back(glyph);
                }
                overrides.push_back(std::move(font));
            }
            offset = fileEnd;
        }

        if (overrides.empty() || atlases.empty())
        {
            Log("font payload is incomplete: metrics=" + std::to_string(overrides.size()) + " atlases=" + std::to_string(atlases.size()));
            return false;
        }
        g_fontOverrides = std::move(overrides);
        g_fontAtlases = std::move(atlases);
        g_fontPayloadLoaded = true;
        g_payloadLoaded = true;
        if (g_logPath.empty())
            g_logPath = path.parent_path() / "t6_translation.log";
        Log("font payload parsed from " + path.string() + " fonts=" + std::to_string(g_fontOverrides.size())
            + " atlases=" + std::to_string(g_fontAtlases.size()));
        return true;
    }

    bool LoadTextPayloadFromPath(const std::filesystem::path& path)
    {
        const auto data = ReadAllBytes(path);
        if (data.size() < 16 || memcmp(data.data(), "T6TRTXT1", 8) != 0)
            return false;
        ParseRuntimeConfig(data);

        Payload payload;
        size_t offset = 8;
        uint32_t localizeCount = 0;
        uint32_t stringTableCount = 0;
        if (!ReadU32(data, offset, localizeCount) || !ReadU32(data, offset, stringTableCount))
            return false;

        payload.localize.reserve(localizeCount);
        for (uint32_t i = 0; i < localizeCount; ++i)
        {
            LocalizeRecord record;
            if (!ReadString16(data, offset, record.name) || !ReadString32(data, offset, record.original) || !ReadString32(data, offset, record.translated))
                return false;
            payload.localize.push_back(std::move(record));
        }

        payload.stringTables.reserve(stringTableCount);
        for (uint32_t i = 0; i < stringTableCount; ++i)
        {
            StringTableRecord record;
            if (!ReadString16(data, offset, record.tableName) || !ReadU32(data, offset, record.row) || !ReadU32(data, offset, record.column)
                || !ReadString32(data, offset, record.original) || !ReadString32(data, offset, record.translated))
            {
                return false;
            }
            payload.stringTables.push_back(std::move(record));
        }

        for (size_t i = 0; i < payload.localize.size(); ++i)
            payload.localizeByName[NormalizeKey(payload.localize[i].name)].push_back(i);
        for (size_t i = 0; i < payload.stringTables.size(); ++i)
            payload.stringTablesByName[payload.stringTables[i].tableName].push_back(i);

        g_payload = std::move(payload);
        g_payloadLoaded = true;
        g_textPayloadLoaded = true;
        g_logPath = path.parent_path() / "t6_translation.log";
        Log("text payload loaded: " + path.string());
        Log("localize records: " + std::to_string(g_payload.localize.size()) + ", stringtable records: " + std::to_string(g_payload.stringTables.size()));
        ParseFontPayload(data, path);
        return true;
    }

    bool LoadFontPayloadFromPath(const std::filesystem::path& path)
    {
        const auto data = ReadAllBytes(path);
        if (data.size() < 8)
            return false;
        ParseRuntimeConfig(data);
        return ParseFontPayload(data, path);
    }

    bool LoadPayload()
    {
        if (g_logPath.empty())
            g_logPath = ModuleDirectory() / "t6_translation.log";
        if (!g_startLogged)
        {
            g_startLogged = true;
            Log("runtime started: font patch mode=entry-only");
        }

        const auto envPath = GetEnvW(L"T6TR_PAYLOAD");
        if (!envPath.empty())
        {
            const auto path = std::filesystem::path(envPath);
            LoadTextPayloadFromPath(path);
            LoadFontPayloadFromPath(path);
        }

        const auto moduleDir = ModuleDirectory();

        const auto allPath = moduleDir / "all.bin";
        if (!g_textPayloadLoaded)
            LoadTextPayloadFromPath(allPath);
        if (!g_fontPayloadLoaded)
            LoadFontPayloadFromPath(allPath);

        if (!g_textPayloadLoaded)
            LoadTextPayloadFromPath(moduleDir / "text.bin");
        if (!g_textPayloadLoaded)
            LoadTextPayloadFromPath(moduleDir / "dll.bin");
        if (!g_fontPayloadLoaded)
            LoadFontPayloadFromPath(moduleDir / "font.bin");

        if (g_payloadLoaded)
        {
            Log("payload status text=" + std::to_string(g_textPayloadLoaded ? 1 : 0) + " font=" + std::to_string(g_fontPayloadLoaded ? 1 : 0));
            return true;
        }

        Log("payload not found");
        return false;
    }

    HMODULE g_realXInput = nullptr;

    FARPROC LoadRealXInputProc(LPCSTR name)
    {
        if (!g_realXInput)
        {
            wchar_t systemDir[MAX_PATH] = {};
            GetSystemDirectoryW(systemDir, MAX_PATH);
            lstrcatW(systemDir, L"\\xinput1_3.dll");
            g_realXInput = LoadLibraryW(systemDir);
        }
        return g_realXInput ? GetProcAddress(g_realXInput, name) : nullptr;
    }

    FARPROC LoadRealXInputOrdinal(WORD ordinal)
    {
        if (!g_realXInput)
        {
            wchar_t systemDir[MAX_PATH] = {};
            GetSystemDirectoryW(systemDir, MAX_PATH);
            lstrcatW(systemDir, L"\\xinput1_3.dll");
            g_realXInput = LoadLibraryW(systemDir);
        }
        return g_realXInput ? GetProcAddress(g_realXInput, reinterpret_cast<LPCSTR>(ordinal)) : nullptr;
    }

    std::vector<Region> BuildRegions()
    {
        SYSTEM_INFO info = {};
        GetSystemInfo(&info);

        std::vector<Region> regions;
        uintptr_t current = reinterpret_cast<uintptr_t>(info.lpMinimumApplicationAddress);
        const uintptr_t maxAddress = reinterpret_cast<uintptr_t>(info.lpMaximumApplicationAddress);

        while (current < maxAddress)
        {
            MEMORY_BASIC_INFORMATION mbi = {};
            if (!VirtualQuery(reinterpret_cast<void*>(current), &mbi, sizeof(mbi)))
                break;

            const uintptr_t begin = reinterpret_cast<uintptr_t>(mbi.BaseAddress);
            const uintptr_t end = begin + mbi.RegionSize;
            if (mbi.State == MEM_COMMIT && IsReadableProtect(mbi.Protect))
                regions.push_back({begin, end, IsWritableProtect(mbi.Protect)});

            current = end;
        }

        std::sort(regions.begin(), regions.end(), [](const Region& a, const Region& b) { return a.begin < b.begin; });
        return regions;
    }

    const Region* FindRegion(const std::vector<Region>& regions, uintptr_t address)
    {
        size_t lo = 0;
        size_t hi = regions.size();
        while (lo < hi)
        {
            const size_t mid = (lo + hi) / 2;
            if (regions[mid].end <= address)
                lo = mid + 1;
            else
                hi = mid;
        }

        if (lo < regions.size() && regions[lo].begin <= address && address < regions[lo].end)
            return &regions[lo];
        return nullptr;
    }

    bool IsReadableRange(const std::vector<Region>& regions, uintptr_t address, size_t size)
    {
        if (!address || !size)
            return false;
        const auto* region = FindRegion(regions, address);
        return region && address + size <= region->end;
    }

    bool TryReadU32(uintptr_t address, uint32_t& value)
    {
        __try
        {
            value = *reinterpret_cast<const uint32_t*>(address);
            return true;
        }
        __except (EXCEPTION_EXECUTE_HANDLER)
        {
            return false;
        }
    }

    bool SafeGetShaderResource(ID3D11ShaderResourceView* view, ID3D11Resource** resource)
    {
        if (!view || !resource)
            return false;
        __try
        {
            view->GetResource(resource);
            return true;
        }
        __except (EXCEPTION_EXECUTE_HANDLER)
        {
            return false;
        }
    }

    bool TryReadS32(uintptr_t address, int32_t& value)
    {
        __try
        {
            value = *reinterpret_cast<const int32_t*>(address);
            return true;
        }
        __except (EXCEPTION_EXECUTE_HANDLER)
        {
            return false;
        }
    }

    bool TryReadCString(const std::vector<Region>& regions, uint32_t pointer, size_t maxLength, std::string& value)
    {
        value.clear();
        if (!pointer || maxLength > 65536 || !IsReadableRange(regions, pointer, 1))
            return false;

        for (size_t i = 0; i < maxLength; ++i)
        {
            const uintptr_t address = static_cast<uintptr_t>(pointer) + i;
            if (!IsReadableRange(regions, address, 1))
                return false;

            char c = 0;
            __try
            {
                c = *reinterpret_cast<const char*>(address);
            }
            __except (EXCEPTION_EXECUTE_HANDLER)
            {
                return false;
            }

            if (c == '\0')
                return true;
            value.push_back(c);
        }

        return false;
    }

    bool WriteU32(uintptr_t address, uint32_t value)
    {
        DWORD oldProtect = 0;
        if (!VirtualProtect(reinterpret_cast<void*>(address), sizeof(value), PAGE_READWRITE, &oldProtect))
            return false;

        __try
        {
            *reinterpret_cast<uint32_t*>(address) = value;
        }
        __except (EXCEPTION_EXECUTE_HANDLER)
        {
            DWORD ignored = 0;
            VirtualProtect(reinterpret_cast<void*>(address), sizeof(value), oldProtect, &ignored);
            return false;
        }

        DWORD ignored = 0;
        VirtualProtect(reinterpret_cast<void*>(address), sizeof(value), oldProtect, &ignored);
        return true;
    }

    bool WriteU16(uintptr_t address, uint16_t value)
    {
        DWORD oldProtect = 0;
        if (!VirtualProtect(reinterpret_cast<void*>(address), sizeof(value), PAGE_READWRITE, &oldProtect))
            return false;
        __try
        {
            *reinterpret_cast<uint16_t*>(address) = value;
        }
        __except (EXCEPTION_EXECUTE_HANDLER)
        {
            DWORD ignored = 0;
            VirtualProtect(reinterpret_cast<void*>(address), sizeof(value), oldProtect, &ignored);
            return false;
        }
        DWORD ignored = 0;
        VirtualProtect(reinterpret_cast<void*>(address), sizeof(value), oldProtect, &ignored);
        return true;
    }

    bool WriteS32(uintptr_t address, int32_t value)
    {
        return WriteU32(address, static_cast<uint32_t>(value));
    }

    ReturnStack* GetReturnStack()
    {
        if (g_returnStackTls == TLS_OUT_OF_INDEXES)
            return nullptr;

        auto* stack = static_cast<ReturnStack*>(TlsGetValue(g_returnStackTls));
        if (!stack)
        {
            stack = static_cast<ReturnStack*>(HeapAlloc(GetProcessHeap(), HEAP_ZERO_MEMORY, sizeof(ReturnStack)));
            if (stack)
                TlsSetValue(g_returnStackTls, stack);
        }
        return stack;
    }

    extern "C" void __stdcall PushOriginalReturn(uintptr_t value)
    {
        auto* stack = GetReturnStack();
        if (!stack || stack->depth >= static_cast<int>(std::size(stack->values)))
            return;
        stack->values[stack->depth++] = value;
    }

    extern "C" uintptr_t __stdcall PopOriginalReturn()
    {
        auto* stack = GetReturnStack();
        if (!stack || stack->depth <= 0)
            return 0;
        return stack->values[--stack->depth];
    }

    bool IsReadableAddress(uintptr_t address, size_t size)
    {
        if (!address || !size)
            return false;

        MEMORY_BASIC_INFORMATION mbi = {};
        if (!VirtualQuery(reinterpret_cast<void*>(address), &mbi, sizeof(mbi)))
            return false;
        if (mbi.State != MEM_COMMIT || !IsReadableProtect(mbi.Protect))
            return false;

        const uintptr_t begin = reinterpret_cast<uintptr_t>(mbi.BaseAddress);
        const uintptr_t end = begin + mbi.RegionSize;
        return address >= begin && address + size <= end;
    }

    bool TryReadCStringRaw(uint32_t pointer, size_t maxLength, std::string& value)
    {
        value.clear();
        if (!pointer || maxLength > 65536)
            return false;

        for (size_t i = 0; i < maxLength; ++i)
        {
            const uintptr_t address = static_cast<uintptr_t>(pointer) + i;
            if (!IsReadableAddress(address, 1))
                return false;

            char c = 0;
            __try
            {
                c = *reinterpret_cast<const char*>(address);
            }
            __except (EXCEPTION_EXECUTE_HANDLER)
            {
                return false;
            }

            if (c == '\0')
                return true;
            value.push_back(c);
        }

        return false;
    }

    int32_t ComHashString(const char* text)
    {
        if (!text)
            return 0;

        uint32_t hash = 5381;
        for (const auto* p = reinterpret_cast<const unsigned char*>(text); *p; ++p)
            hash = ((hash << 5) + hash) + static_cast<unsigned char>(tolower(*p));
        return static_cast<int32_t>(hash);
    }

    bool MatchesCurrentValue(const std::string& current, const std::string& original, const std::string& translated)
    {
        return current == original || current == translated;
    }

    bool TryPatchLocalizeAt(const std::vector<Region>& regions,
                            uintptr_t address,
                            uint32_t valuePtr,
                            uint32_t namePtr,
                            int& candidates,
                            int& matched,
                            int& patched)
    {
        std::string name;
        std::string current;

        if (!TryReadCString(regions, namePtr, 256, name))
            return false;

        const auto found = g_payload.localizeByName.find(NormalizeKey(name));
        if (found == g_payload.localizeByName.end())
            return false;

        ++candidates;
        for (const size_t recordIndex : found->second)
        {
            const auto& record = g_payload.localize[recordIndex];
            const size_t maxLength = (std::max)(record.original.size(), record.translated.size()) + 1;
            if (!TryReadCString(regions, valuePtr, maxLength, current))
                continue;

            if (!MatchesCurrentValue(current, record.original, record.translated))
                continue;

            ++matched;
            const auto translatedPtr = reinterpret_cast<uint32_t>(record.translated.c_str());
            if (valuePtr != translatedPtr && WriteU32(address, translatedPtr))
                ++patched;
            return true;
        }

        return false;
    }

    int ApplyLocalize(const std::vector<Region>& regions, int& candidates, int& matched)
    {
        int patched = 0;
        candidates = 0;
        matched = 0;

        for (const auto& region : regions)
        {
            for (uintptr_t address = region.begin; address + 8 <= region.end; address += 4)
            {
                uint32_t firstPtr = 0;
                uint32_t secondPtr = 0;
                if (!TryReadU32(address, firstPtr) || !TryReadU32(address + 4, secondPtr))
                    continue;

                if (TryPatchLocalizeAt(regions, address, firstPtr, secondPtr, candidates, matched, patched))
                    continue;

                TryPatchLocalizeAt(regions, address + 4, secondPtr, firstPtr, candidates, matched, patched);
            }
        }

        return patched;
    }

    void RebuildCellIndex(const StringTable32& table, uint32_t cellCount)
    {
        if (!table.cellIndex || cellCount > 65535)
            return;

        std::vector<uint16_t> indices(cellCount);
        for (uint32_t i = 0; i < cellCount; ++i)
            indices[i] = static_cast<uint16_t>(i);

        const auto* cells = reinterpret_cast<const StringTableCell32*>(table.values);
        std::sort(indices.begin(),
                  indices.end(),
                  [&](uint16_t a, uint16_t b)
                  {
                      const auto& cellA = cells[a];
                      const auto& cellB = cells[b];
                      if (cellA.hash != cellB.hash)
                          return cellA.hash < cellB.hash;
                      return (a % table.columnCount) < (b % table.columnCount);
                  });

        DWORD oldProtect = 0;
        const size_t byteCount = indices.size() * sizeof(uint16_t);
        if (!VirtualProtect(reinterpret_cast<void*>(table.cellIndex), byteCount, PAGE_READWRITE, &oldProtect))
            return;

        memcpy(reinterpret_cast<void*>(table.cellIndex), indices.data(), byteCount);

        DWORD ignored = 0;
        VirtualProtect(reinterpret_cast<void*>(table.cellIndex), byteCount, oldProtect, &ignored);
    }

    int ApplyStringTables(const std::vector<Region>& regions, int& tableCandidates, int& cellMatched)
    {
        int patched = 0;
        tableCandidates = 0;
        cellMatched = 0;
        std::string tableName;
        std::string current;

        for (const auto& region : regions)
        {
            for (uintptr_t address = region.begin; address + sizeof(StringTable32) <= region.end; address += 4)
            {
                StringTable32 table = {};
                if (!TryReadU32(address, table.name) || !TryReadS32(address + 4, table.columnCount) || !TryReadS32(address + 8, table.rowCount)
                    || !TryReadU32(address + 12, table.values) || !TryReadU32(address + 16, table.cellIndex))
                {
                    continue;
                }

                if (table.columnCount <= 0 || table.columnCount > 64 || table.rowCount <= 0 || table.rowCount > 10000 || !table.values)
                    continue;

                if (!TryReadCString(regions, table.name, 260, tableName))
                    continue;

                const auto found = g_payload.stringTablesByName.find(tableName);
                if (found == g_payload.stringTablesByName.end())
                    continue;
                ++tableCandidates;

                const uint32_t cellCount = static_cast<uint32_t>(table.columnCount) * static_cast<uint32_t>(table.rowCount);
                if (!IsReadableRange(regions, table.values, sizeof(StringTableCell32)))
                    continue;

                bool tableChanged = false;
                for (const size_t recordIndex : found->second)
                {
                    const auto& record = g_payload.stringTables[recordIndex];
                    if (record.column >= static_cast<uint32_t>(table.columnCount) || record.row >= static_cast<uint32_t>(table.rowCount))
                        continue;

                    const uint32_t cellIndex = record.row * static_cast<uint32_t>(table.columnCount) + record.column;
                    if (cellIndex >= cellCount)
                        continue;

                    const uintptr_t cellAddress = table.values + static_cast<uintptr_t>(cellIndex) * sizeof(StringTableCell32);
                    if (!IsReadableRange(regions, cellAddress, sizeof(StringTableCell32)))
                        continue;

                    uint32_t stringPtr = 0;
                    if (!TryReadU32(cellAddress, stringPtr))
                        continue;

                    const size_t maxLength = std::max(record.original.size(), record.translated.size()) + 1;
                    if (!TryReadCString(regions, stringPtr, maxLength, current))
                        continue;

                    if (!MatchesCurrentValue(current, record.original, record.translated))
                        continue;

                    ++cellMatched;
                    const auto translatedPtr = reinterpret_cast<uint32_t>(record.translated.c_str());
                    const int32_t translatedHash = ComHashString(record.translated.c_str());
                    if (stringPtr != translatedPtr)
                    {
                        if (WriteU32(cellAddress, translatedPtr))
                        {
                            WriteS32(cellAddress + 4, translatedHash);
                            ++patched;
                            tableChanged = true;
                        }
                    }
                }

                if (tableChanged)
                    RebuildCellIndex(table, cellCount);
            }
        }

        return patched;
    }

    bool ReadPoolInfo(const std::vector<Region>& regions, int assetType, uint32_t& pool, uint32_t& capacity)
    {
        const uintptr_t poolPointers = RebaseT6Va(T6_POOL_POINTERS_VA);
        const uintptr_t poolCapacities = RebaseT6Va(T6_POOL_CAPACITIES_VA);
        if (!IsReadableRange(regions, poolPointers + static_cast<uintptr_t>(assetType) * 4, 4)
            || !IsReadableRange(regions, poolCapacities + static_cast<uintptr_t>(assetType) * 4, 4))
        {
            return false;
        }

        return TryReadU32(poolPointers + static_cast<uintptr_t>(assetType) * 4, pool)
            && TryReadU32(poolCapacities + static_cast<uintptr_t>(assetType) * 4, capacity) && pool != 0 && capacity != 0;
    }

    int ApplyLocalizePool(const std::vector<Region>& regions, uint32_t pool, uint32_t capacity, int& candidates, int& matched)
    {
        int patched = 0;
        candidates = 0;
        matched = 0;

        const uint32_t maxCapacity = (std::min)(capacity, 0x40000u);
        for (uint32_t i = 0; i < maxCapacity; ++i)
        {
            const uintptr_t address = static_cast<uintptr_t>(pool) + static_cast<uintptr_t>(i) * 8;
            if (!IsReadableRange(regions, address, 8))
                break;

            uint32_t firstPtr = 0;
            uint32_t secondPtr = 0;
            if (!TryReadU32(address, firstPtr) || !TryReadU32(address + 4, secondPtr))
                continue;

            if (TryPatchLocalizeAt(regions, address, firstPtr, secondPtr, candidates, matched, patched))
                continue;

            TryPatchLocalizeAt(regions, address + 4, secondPtr, firstPtr, candidates, matched, patched);
        }

        return patched;
    }

    int ApplyLocalizePoolLoose(const std::vector<Region>& regions, uint32_t pool, uint32_t capacity, int& keyPointers, int& matched)
    {
        int patched = 0;
        keyPointers = 0;
        matched = 0;

        const uintptr_t begin = pool;
        const uintptr_t byteCount = static_cast<uintptr_t>((std::min)(capacity, 0x40000u)) * 64u;
        const uintptr_t end = begin + byteCount;
        std::string possibleKey;
        std::string possibleValue;
        int loggedSamples = 0;

        for (uintptr_t address = begin; address + 4 <= end; address += 4)
        {
            if (!IsReadableRange(regions, address, 4))
                break;

            uint32_t keyPtr = 0;
            if (!TryReadU32(address, keyPtr))
                continue;

            if (!TryReadCString(regions, keyPtr, 256, possibleKey))
                continue;

            const auto found = g_payload.localizeByName.find(NormalizeKey(possibleKey));
            if (found == g_payload.localizeByName.end())
                continue;

            ++keyPointers;
            if (loggedSamples < 8)
            {
                Log("localize key sample offset=" + std::to_string(address - begin) + " key=" + possibleKey);
                ++loggedSamples;
            }

            for (int neighbor = -4; neighbor <= 4; ++neighbor)
            {
                if (neighbor == 0)
                    continue;

                const auto signedValueAddress = static_cast<int64_t>(address) + static_cast<int64_t>(neighbor) * 4;
                if (signedValueAddress < static_cast<int64_t>(begin))
                    continue;

                const uintptr_t valueAddress = static_cast<uintptr_t>(signedValueAddress);
                if (!IsReadableRange(regions, valueAddress, 4))
                    continue;

                uint32_t valuePtr = 0;
                if (!TryReadU32(valueAddress, valuePtr))
                    continue;

                for (const size_t recordIndex : found->second)
                {
                    const auto& record = g_payload.localize[recordIndex];
                    const size_t maxLength = (std::max)(record.original.size(), record.translated.size()) + 1;
                    if (!TryReadCString(regions, valuePtr, maxLength, possibleValue))
                        continue;

                    if (!MatchesCurrentValue(possibleValue, record.original, record.translated))
                        continue;

                    ++matched;
                    const auto translatedPtr = reinterpret_cast<uint32_t>(record.translated.c_str());
                    if (valuePtr != translatedPtr && WriteU32(valueAddress, translatedPtr))
                        ++patched;
                    break;
                }
            }
        }

        return patched;
    }

    bool TryPatchLocalizeHeader(const std::vector<Region>& regions,
                                uint32_t header,
                                int& candidates,
                                int& matched,
                                int& patched,
                                int& samplesLogged);
    bool TryPatchStringTableHeader(const std::vector<Region>& regions, uint32_t header, int& tableCandidates, int& cellMatched, int& patched);

    int ApplyLocalizePointerPool(const std::vector<Region>& regions, uint32_t pool, uint32_t capacity, int& candidates, int& matched)
    {
        int patched = 0;
        int samplesLogged = 0;
        candidates = 0;
        matched = 0;
        std::unordered_set<uint32_t> seen;

        const uint32_t maxCapacity = (std::min)(capacity, 0x40000u);
        for (uint32_t i = 0; i < maxCapacity; ++i)
        {
            const uintptr_t pointerAddress = static_cast<uintptr_t>(pool) + static_cast<uintptr_t>(i) * 4u;
            if (!IsReadableRange(regions, pointerAddress, 4))
                break;

            uint32_t header = 0;
            if (!TryReadU32(pointerAddress, header) || !header || !seen.insert(header).second)
                continue;

            TryPatchLocalizeHeader(regions, header, candidates, matched, patched, samplesLogged);
        }

        if (g_scanPass <= 2)
            Log("localize pointer pool patched=" + std::to_string(patched) + " candidates=" + std::to_string(candidates)
                + " matched=" + std::to_string(matched));
        return patched;
    }

    int ApplyStringTablePool(const std::vector<Region>& regions, uint32_t pool, uint32_t capacity, int& tableCandidates, int& cellMatched)
    {
        int patched = 0;
        tableCandidates = 0;
        cellMatched = 0;
        std::string tableName;
        std::string current;

        const uint32_t maxCapacity = (std::min)(capacity, 4096u);
        for (uint32_t i = 0; i < maxCapacity; ++i)
        {
            const uintptr_t address = static_cast<uintptr_t>(pool) + static_cast<uintptr_t>(i) * sizeof(StringTable32);
            if (!IsReadableRange(regions, address, sizeof(StringTable32)))
                break;

            StringTable32 table = {};
            if (!TryReadU32(address, table.name) || !TryReadS32(address + 4, table.columnCount) || !TryReadS32(address + 8, table.rowCount)
                || !TryReadU32(address + 12, table.values) || !TryReadU32(address + 16, table.cellIndex))
            {
                continue;
            }

            if (table.columnCount <= 0 || table.columnCount > 64 || table.rowCount <= 0 || table.rowCount > 10000 || !table.values)
                continue;

            if (!TryReadCString(regions, table.name, 260, tableName))
                continue;

            const auto found = g_payload.stringTablesByName.find(tableName);
            if (found == g_payload.stringTablesByName.end())
                continue;

            ++tableCandidates;
            const uint32_t cellCount = static_cast<uint32_t>(table.columnCount) * static_cast<uint32_t>(table.rowCount);
            bool tableChanged = false;

            for (const size_t recordIndex : found->second)
            {
                const auto& record = g_payload.stringTables[recordIndex];
                if (record.column >= static_cast<uint32_t>(table.columnCount) || record.row >= static_cast<uint32_t>(table.rowCount))
                    continue;

                const uint32_t cellIndex = record.row * static_cast<uint32_t>(table.columnCount) + record.column;
                if (cellIndex >= cellCount)
                    continue;

                const uintptr_t cellAddress = table.values + static_cast<uintptr_t>(cellIndex) * sizeof(StringTableCell32);
                if (!IsReadableRange(regions, cellAddress, sizeof(StringTableCell32)))
                    continue;

                uint32_t stringPtr = 0;
                if (!TryReadU32(cellAddress, stringPtr))
                    continue;

                const size_t maxLength = (std::max)(record.original.size(), record.translated.size()) + 1;
                if (!TryReadCString(regions, stringPtr, maxLength, current))
                    continue;

                if (!MatchesCurrentValue(current, record.original, record.translated))
                    continue;

                ++cellMatched;
                const auto translatedPtr = reinterpret_cast<uint32_t>(record.translated.c_str());
                const int32_t translatedHash = ComHashString(record.translated.c_str());
                if (stringPtr != translatedPtr && WriteU32(cellAddress, translatedPtr))
                {
                    WriteS32(cellAddress + 4, translatedHash);
                    ++patched;
                    tableChanged = true;
                }
            }

            if (tableChanged)
                RebuildCellIndex(table, cellCount);
        }

        return patched;
    }

    int ApplyStringTablePointerPool(const std::vector<Region>& regions, uint32_t pool, uint32_t capacity, int& tableCandidates, int& cellMatched)
    {
        int patched = 0;
        tableCandidates = 0;
        cellMatched = 0;
        std::unordered_set<uint32_t> seen;

        const uint32_t maxCapacity = (std::min)(capacity, 4096u);
        for (uint32_t i = 0; i < maxCapacity; ++i)
        {
            const uintptr_t pointerAddress = static_cast<uintptr_t>(pool) + static_cast<uintptr_t>(i) * 4u;
            if (!IsReadableRange(regions, pointerAddress, 4))
                break;

            uint32_t header = 0;
            if (!TryReadU32(pointerAddress, header) || !header || !seen.insert(header).second)
                continue;

            TryPatchStringTableHeader(regions, header, tableCandidates, cellMatched, patched);
        }

        if (g_scanPass <= 2)
            Log("stringtable pointer pool patched=" + std::to_string(patched) + " candidates=" + std::to_string(tableCandidates)
                + " matched=" + std::to_string(cellMatched));
        return patched;
    }

    bool TryPatchLocalizeHeader(const std::vector<Region>& regions,
                                uint32_t header,
                                int& candidates,
                                int& matched,
                                int& patched,
                                int& samplesLogged)
    {
        if (!IsReadableRange(regions, header, 8))
            return false;

        uint32_t firstPtr = 0;
        uint32_t secondPtr = 0;
        if (!TryReadU32(header, firstPtr) || !TryReadU32(static_cast<uintptr_t>(header) + 4, secondPtr))
            return false;

        std::string first;
        std::string second;
        const bool firstReadable = TryReadCString(regions, firstPtr, 512, first);
        const bool secondReadable = TryReadCString(regions, secondPtr, 512, second);

        struct Candidate
        {
            uint32_t namePtr;
            uint32_t valuePtr;
            uintptr_t valueField;
            std::string name;
        };

        Candidate candidatesToTry[2] = {
            {secondPtr, firstPtr, header, second},
            {firstPtr, secondPtr, static_cast<uintptr_t>(header) + 4, first},
        };

        if (!firstReadable && !secondReadable)
            return false;

        for (const auto& candidate : candidatesToTry)
        {
            if (candidate.name.empty())
                continue;

            const auto found = g_payload.localizeByName.find(NormalizeKey(candidate.name));
            if (found == g_payload.localizeByName.end())
                continue;

            ++candidates;
            if (samplesLogged < 12)
            {
                Log("xasset localize sample key=" + candidate.name + " header=" + std::to_string(header));
                ++samplesLogged;
            }

            std::string current;
            for (const size_t recordIndex : found->second)
            {
                const auto& record = g_payload.localize[recordIndex];
                const size_t maxLength = (std::max)(record.original.size(), record.translated.size()) + 1;
                if (!TryReadCString(regions, candidate.valuePtr, maxLength, current))
                    continue;

                if (!MatchesCurrentValue(current, record.original, record.translated))
                    continue;

                ++matched;
                const auto translatedPtr = reinterpret_cast<uint32_t>(record.translated.c_str());
                if (candidate.valuePtr != translatedPtr && WriteU32(candidate.valueField, translatedPtr))
                    ++patched;
                return true;
            }
        }

        return false;
    }

    bool TryPatchStringTableHeader(const std::vector<Region>& regions, uint32_t header, int& tableCandidates, int& cellMatched, int& patched)
    {
        if (!IsReadableRange(regions, header, sizeof(StringTable32)))
            return false;

        StringTable32 table = {};
        if (!TryReadU32(header, table.name) || !TryReadS32(static_cast<uintptr_t>(header) + 4, table.columnCount)
            || !TryReadS32(static_cast<uintptr_t>(header) + 8, table.rowCount) || !TryReadU32(static_cast<uintptr_t>(header) + 12, table.values)
            || !TryReadU32(static_cast<uintptr_t>(header) + 16, table.cellIndex))
        {
            return false;
        }

        if (table.columnCount <= 0 || table.columnCount > 64 || table.rowCount <= 0 || table.rowCount > 10000 || !table.values)
            return false;

        std::string tableName;
        TryReadCString(regions, table.name, 260, tableName);

        const auto found = g_payload.stringTablesByName.find(tableName);
        bool tableChanged = false;
        const uint32_t cellCount = static_cast<uint32_t>(table.columnCount) * static_cast<uint32_t>(table.rowCount);

        if (found != g_payload.stringTablesByName.end())
        {
            ++tableCandidates;
            for (const size_t recordIndex : found->second)
            {
                const auto& record = g_payload.stringTables[recordIndex];
                if (record.column >= static_cast<uint32_t>(table.columnCount) || record.row >= static_cast<uint32_t>(table.rowCount))
                    continue;

                const uint32_t cellIndex = record.row * static_cast<uint32_t>(table.columnCount) + record.column;
                if (cellIndex >= cellCount)
                    continue;

                const uintptr_t cellAddress = table.values + static_cast<uintptr_t>(cellIndex) * sizeof(StringTableCell32);
                if (!IsReadableRange(regions, cellAddress, sizeof(StringTableCell32)))
                    continue;

                uint32_t stringPtr = 0;
                if (!TryReadU32(cellAddress, stringPtr))
                    continue;

                std::string current;
                const size_t maxLength = (std::max)(record.original.size(), record.translated.size()) + 1;
                if (!TryReadCString(regions, stringPtr, maxLength, current))
                    continue;

                if (!MatchesCurrentValue(current, record.original, record.translated))
                    continue;

                ++cellMatched;
                const auto translatedPtr = reinterpret_cast<uint32_t>(record.translated.c_str());
                if (stringPtr != translatedPtr && WriteU32(cellAddress, translatedPtr))
                {
                    WriteS32(cellAddress + 4, ComHashString(record.translated.c_str()));
                    ++patched;
                    tableChanged = true;
                }
            }
        }

        if (tableChanged)
            RebuildCellIndex(table, cellCount);
        return true;
    }

    bool TryPatchLocalizeHeaderRaw(uint32_t header, bool& patchedValue, std::string& patchedKey)
    {
        patchedValue = false;
        patchedKey.clear();

        if (!IsReadableAddress(header, 8))
            return false;

        uint32_t firstPtr = 0;
        uint32_t secondPtr = 0;
        if (!TryReadU32(header, firstPtr) || !TryReadU32(static_cast<uintptr_t>(header) + 4, secondPtr))
            return false;

        std::string first;
        std::string second;
        TryReadCStringRaw(firstPtr, 512, first);
        TryReadCStringRaw(secondPtr, 512, second);

        struct Candidate
        {
            uint32_t namePtr;
            uint32_t valuePtr;
            uintptr_t valueField;
            const std::string* name;
        };

        const Candidate candidatesToTry[2] = {
            {secondPtr, firstPtr, header, &second},
            {firstPtr, secondPtr, static_cast<uintptr_t>(header) + 4, &first},
        };

        for (const auto& candidate : candidatesToTry)
        {
            if (!candidate.name || candidate.name->empty())
                continue;

            const auto found = g_payload.localizeByName.find(NormalizeKey(*candidate.name));
            if (found == g_payload.localizeByName.end())
                continue;

            patchedKey = *candidate.name;
            InterlockedIncrement(&g_hookLocalizeCandidates);

            std::string current;
            for (const size_t recordIndex : found->second)
            {
                const auto& record = g_payload.localize[recordIndex];
                const size_t maxLength = (std::max)(record.original.size(), record.translated.size()) + 1;
                if (!TryReadCStringRaw(candidate.valuePtr, maxLength, current))
                    continue;

                if (!MatchesCurrentValue(current, record.original, record.translated))
                    continue;

                const auto translatedPtr = reinterpret_cast<uint32_t>(record.translated.c_str());
                if (candidate.valuePtr != translatedPtr && WriteU32(candidate.valueField, translatedPtr))
                {
                    patchedValue = true;
                    InterlockedIncrement(&g_hookLocalizePatched);
                }
                return true;
            }
            return true;
        }

        return false;
    }

    bool PatchFontHeader(uintptr_t header, const char* sourceTag, bool logPatch);

    extern "C" void __stdcall OnDBFindXAssetEntryReturn(uint32_t assetType, uint32_t entry)
    {
        const LONG callIndex = InterlockedIncrement(&g_lookupCalls);
        if (callIndex <= 24)
            Log("lookup call type=" + std::to_string(assetType) + " entry=" + std::to_string(entry));

        if (!entry || !g_payloadLoaded)
            return;

        uint32_t header = 0;
        if (!TryReadU32(static_cast<uintptr_t>(entry) + 4, header) || !header)
            return;

        if (assetType == T6_ASSET_LOCALIZE)
        {
            const LONG localizeIndex = InterlockedIncrement(&g_lookupLocalizeCalls);
            bool patchedValue = false;
            std::string key;
            const bool candidate = TryPatchLocalizeHeaderRaw(header, patchedValue, key);
            if (localizeIndex <= 40 || patchedValue)
            {
                Log("lookup localize entry=" + std::to_string(entry) + " header=" + std::to_string(header) + " candidate="
                    + std::to_string(candidate ? 1 : 0) + " patched=" + std::to_string(patchedValue ? 1 : 0)
                    + (key.empty() ? std::string() : (" key=" + key)));
            }
        }
        else if (assetType == T6_ASSET_STRINGTABLE)
        {
            const auto regions = BuildRegions();
            int tableCandidates = 0;
            int cellMatched = 0;
            int patched = 0;
            TryPatchStringTableHeader(regions, header, tableCandidates, cellMatched, patched);
            if (patched)
            {
                InterlockedExchangeAdd(&g_hookStringTablePatched, patched);
                Log("lookup stringtable header=" + std::to_string(header) + " patched=" + std::to_string(patched));
            }
        }
        else if (assetType == T6_ASSET_FONT)
        {
            if (PatchFontHeader(header, "lookup", callIndex <= 40))
                Log("lookup font header=" + std::to_string(header));
        }
    }

    __declspec(naked) void DBFindXAssetEntryPost()
    {
        __asm
        {
            push eax
            pushfd
            pushad
            mov eax, [esp + 36]
            push eax
            push edi
            call OnDBFindXAssetEntryReturn
            add esp, 8
            popad
            popfd
            call PopOriginalReturn
            mov edx, eax
            pop eax
            test edx, edx
            jz original_return_missing
            jmp edx
original_return_missing:
            ret 4
        }
    }

    __declspec(naked) void DBFindXAssetEntryHook()
    {
        __asm
        {
            pushfd
            pushad
            mov eax, [esp + 36]
            push eax
            call PushOriginalReturn
            add esp, 4
            popad
            popfd
            push ebx
            mov ebx, [esp + 8]
            mov dword ptr [esp + 4], offset DBFindXAssetEntryPost
            jmp dword ptr [g_dbFindContinue]
        }
    }

    bool WriteJump(uintptr_t source, uintptr_t destination)
    {
        DWORD oldProtect = 0;
        if (!VirtualProtect(reinterpret_cast<void*>(source), 5, PAGE_EXECUTE_READWRITE, &oldProtect))
            return false;

        const int32_t relative = static_cast<int32_t>(destination - source - 5);
        auto* bytes = reinterpret_cast<uint8_t*>(source);
        bytes[0] = 0xE9;
        memcpy(bytes + 1, &relative, sizeof(relative));
        FlushInstructionCache(GetCurrentProcess(), reinterpret_cast<void*>(source), 5);

        DWORD ignored = 0;
        VirtualProtect(reinterpret_cast<void*>(source), 5, oldProtect, &ignored);
        return true;
    }

    bool InstallDBFindHook()
    {
        if (g_dbFindHookInstalled)
            return true;

        if (g_returnStackTls == TLS_OUT_OF_INDEXES)
        {
            g_returnStackTls = TlsAlloc();
            if (g_returnStackTls == TLS_OUT_OF_INDEXES)
            {
                Log("db_find hook failed: TlsAlloc");
                return false;
            }
        }

        const uintptr_t target = RebaseT6Va(T6_DB_FIND_XASSET_ENTRY_VA);
        if (!IsReadableAddress(target, sizeof(DB_FIND_PROLOGUE)) || memcmp(reinterpret_cast<const void*>(target), DB_FIND_PROLOGUE, sizeof(DB_FIND_PROLOGUE)) != 0)
        {
            Log("db_find hook failed: unexpected prologue at " + std::to_string(target));
            return false;
        }

        g_dbFindContinue = target + sizeof(DB_FIND_PROLOGUE);
        if (!WriteJump(target, reinterpret_cast<uintptr_t>(&DBFindXAssetEntryHook)))
        {
            Log("db_find hook failed: WriteJump");
            return false;
        }

        g_dbFindHookInstalled = true;
        Log("db_find hook installed target=" + std::to_string(target) + " continue=" + std::to_string(g_dbFindContinue));
        return true;
    }

    int ApplyXAssetEntries(const std::vector<Region>& regions,
                           int& localizeCandidates,
                           int& localizeMatched,
                           int& stringTableCandidates,
                           int& stringTableMatched)
    {
        int patched = 0;
        int samplesLogged = 0;
        localizeCandidates = 0;
        localizeMatched = 0;
        stringTableCandidates = 0;
        stringTableMatched = 0;

        const uintptr_t entries = RebaseT6Va(T6_XASSET_ENTRIES_VA);
        if (!IsReadableRange(regions, entries, T6_XASSET_ENTRY_SIZE))
        {
            Log("xasset entries not readable");
            return 0;
        }

        uint32_t localizeEntriesSeen = 0;
        uint32_t stringTableEntriesSeen = 0;
        uint32_t nonZeroEntriesSeen = 0;
        int layoutSamplesLogged = 0;
        for (uint32_t i = 0; i < T6_XASSET_ENTRY_COUNT; ++i)
        {
            const uintptr_t entry = entries + static_cast<uintptr_t>(i) * T6_XASSET_ENTRY_SIZE;
            if (!IsReadableRange(regions, entry, T6_XASSET_ENTRY_SIZE))
                break;

            uint32_t type = 0;
            uint32_t header = 0;
            uint32_t dword2 = 0;
            uint32_t dword3 = 0;
            if (!TryReadU32(entry, type) || !TryReadU32(entry + 4, header) || !TryReadU32(entry + 8, dword2) || !TryReadU32(entry + 12, dword3))
                continue;

            if (type || header || dword2 || dword3)
            {
                ++nonZeroEntriesSeen;
                if (g_scanPass <= 2 && layoutSamplesLogged < 10)
                {
                    Log("xasset sample i=" + std::to_string(i) + " d0=" + std::to_string(type) + " d1=" + std::to_string(header)
                        + " d2=" + std::to_string(dword2) + " d3=" + std::to_string(dword3));
                    ++layoutSamplesLogged;
                }
            }

            if (!header)
                continue;

            if (type == T6_ASSET_LOCALIZE)
            {
                ++localizeEntriesSeen;
                TryPatchLocalizeHeader(regions, header, localizeCandidates, localizeMatched, patched, samplesLogged);
            }
            else if (type == T6_ASSET_STRINGTABLE)
            {
                ++stringTableEntriesSeen;
                TryPatchStringTableHeader(regions, header, stringTableCandidates, stringTableMatched, patched);
            }
        }

        if (g_scanPass <= 2)
        {
            Log("xasset entries seen localize=" + std::to_string(localizeEntriesSeen) + " stringtable=" + std::to_string(stringTableEntriesSeen));
            Log("xasset entries nonzero=" + std::to_string(nonZeroEntriesSeen));
        }

        return patched;
    }

    int ApplyKnownPools(const std::vector<Region>& regions,
                        int& localizeCandidates,
                        int& localizeMatched,
                        int& stringTableCandidates,
                        int& stringTableMatched)
    {
        uint32_t localizePool = 0;
        uint32_t localizeCapacity = 0;
        uint32_t stringTablePool = 0;
        uint32_t stringTableCapacity = 0;

        localizeCandidates = 0;
        localizeMatched = 0;
        stringTableCandidates = 0;
        stringTableMatched = 0;

        const bool hasLocalize = ReadPoolInfo(regions, T6_ASSET_LOCALIZE, localizePool, localizeCapacity);
        const bool hasStringTable = ReadPoolInfo(regions, T6_ASSET_STRINGTABLE, stringTablePool, stringTableCapacity);

        if (g_scanPass <= 2)
        {
            Log("pool info localize=" + std::to_string(hasLocalize ? localizePool : 0) + "/" + std::to_string(localizeCapacity)
                + " stringtable=" + std::to_string(hasStringTable ? stringTablePool : 0) + "/" + std::to_string(stringTableCapacity));
        }

        int patched = 0;
        if (hasLocalize)
        {
            patched += ApplyLocalizePool(regions, localizePool, localizeCapacity, localizeCandidates, localizeMatched);
            if (localizeCandidates == 0 && localizeMatched == 0)
                patched += ApplyLocalizePoolLoose(regions, localizePool, localizeCapacity, localizeCandidates, localizeMatched);
            if (localizeCandidates == 0 && localizeMatched == 0)
                patched += ApplyLocalizePointerPool(regions, localizePool, localizeCapacity, localizeCandidates, localizeMatched);
        }
        if (hasStringTable)
        {
            patched += ApplyStringTablePool(regions, stringTablePool, stringTableCapacity, stringTableCandidates, stringTableMatched);
            if (stringTableCandidates == 0 && stringTableMatched == 0)
                patched += ApplyStringTablePointerPool(regions, stringTablePool, stringTableCapacity, stringTableCandidates, stringTableMatched);
        }

        return patched;
    }

    std::string NormalizeAssetName(std::string value)
    {
        std::replace(value.begin(), value.end(), '\\', '/');
        std::transform(value.begin(), value.end(), value.begin(), [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
        return value;
    }

    FontOverride* FindFontOverride(const std::string& name)
    {
        const std::string normalized = NormalizeAssetName(name);
        for (auto& font : g_fontOverrides)
        {
            if (NormalizeAssetName(font.name) == normalized)
                return &font;
        }
        return nullptr;
    }

    FontAtlasOverride* FindFontAtlas(const std::string& key)
    {
        const std::string normalized = NormalizeAssetName(key);
        for (auto& atlas : g_fontAtlases)
        {
            if (NormalizeAssetName(atlas.key) == normalized)
                return &atlas;
        }
        return nullptr;
    }

    bool EnsureFontAtlasView(uintptr_t imageAddress, const std::string& atlasKey)
    {
        FontAtlasOverride* atlas = FindFontAtlas(atlasKey);
        if (!imageAddress || !atlas || atlas->rgba.empty())
            return false;
        if (atlas->view)
            return WriteU32(imageAddress, reinterpret_cast<uint32_t>(atlas->view));

        uint32_t oldViewAddress = 0;
        if (!TryReadU32(imageAddress, oldViewAddress) || !oldViewAddress)
            return false;
        auto* oldView = reinterpret_cast<ID3D11ShaderResourceView*>(oldViewAddress);
        ID3D11Resource* oldResource = nullptr;
        if (!SafeGetShaderResource(oldView, &oldResource))
            return false;
        if (!oldResource)
            return false;
        ID3D11Device* device = nullptr;
        oldResource->GetDevice(&device);
        oldResource->Release();
        if (!device)
            return false;

        D3D11_TEXTURE2D_DESC description = {};
        description.Width = atlas->width;
        description.Height = atlas->height;
        description.MipLevels = 1;
        description.ArraySize = 1;
        description.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
        description.SampleDesc.Count = 1;
        description.Usage = D3D11_USAGE_IMMUTABLE;
        description.BindFlags = D3D11_BIND_SHADER_RESOURCE;
        D3D11_SUBRESOURCE_DATA initialData = {};
        initialData.pSysMem = atlas->rgba.data();
        initialData.SysMemPitch = atlas->width * 4;

        ID3D11Texture2D* texture = nullptr;
        HRESULT result = device->CreateTexture2D(&description, &initialData, &texture);
        if (SUCCEEDED(result) && texture)
        {
            result = device->CreateShaderResourceView(texture, nullptr, &atlas->view);
            texture->Release();
        }
        device->Release();
        if (FAILED(result) || !atlas->view)
        {
            Log("font atlas texture creation failed hr=" + std::to_string(static_cast<uint32_t>(result)));
            return false;
        }

        WriteU16(imageAddress + 20, static_cast<uint16_t>(atlas->width));
        WriteU16(imageAddress + 22, static_cast<uint16_t>(atlas->height));
        Log("font atlas texture created key=" + atlas->key + " " + std::to_string(atlas->width) + "x" + std::to_string(atlas->height));
        return WriteU32(imageAddress, reinterpret_cast<uint32_t>(atlas->view));
    }

    bool PatchFontMaterialAtlas(uintptr_t materialAddress, const std::string& atlasKey)
    {
        if (!materialAddress)
            return false;
        uint32_t textureTable = 0;
        uint32_t image = 0;
        if (!TryReadU32(materialAddress + 96, textureTable) || !textureTable || !TryReadU32(textureTable + 12, image) || !image)
            return false;
        return EnsureFontAtlasView(image, atlasKey);
    }

    bool PatchFontHeader(uintptr_t header, const char* sourceTag, bool logPatch)
    {
        if (!g_fontPayloadLoaded || !header || !IsReadableAddress(header, 36))
            return false;

        uint32_t nameAddress = 0;
        std::string name;
        if (!TryReadU32(header, nameAddress) || !nameAddress || !TryReadCStringRaw(nameAddress, 128, name))
            return false;

        FontOverride* font = FindFontOverride(name);
        if (!font || font->glyphs.empty())
            return false;

        uint32_t oldGlyphCount = 0;
        uint32_t material = 0;
        uint32_t glowMaterial = 0;
        TryReadU32(header + 12, oldGlyphCount);
        TryReadU32(header + 20, material);
        TryReadU32(header + 24, glowMaterial);

        bool changed = false;
        changed |= WriteU32(header + 4, static_cast<uint32_t>(font->pixelHeight));
        changed |= WriteU32(header + 8, static_cast<uint32_t>(font->isScalingAllowed));
        changed |= WriteU32(header + 12, static_cast<uint32_t>(font->glyphs.size()));
        changed |= WriteU32(header + 28, reinterpret_cast<uint32_t>(font->glyphs.data()));
        changed |= PatchFontMaterialAtlas(material, font->atlasKey);
        changed |= PatchFontMaterialAtlas(glowMaterial, font->atlasKey);

        if (changed)
        {
            InterlockedIncrement(&g_hookFontPatched);
            if (logPatch)
            {
                Log(std::string("font patched source=") + sourceTag + " name=" + name + " oldGlyphs="
                    + std::to_string(oldGlyphCount) + " newGlyphs=" + std::to_string(font->glyphs.size()));
            }
        }
        return changed;
    }

    int ApplyFontEntryTable(const std::vector<Region>& regions)
    {
        if (!g_fontPayloadLoaded)
            return 0;
        const uintptr_t entries = RebaseT6Va(T6_XASSET_ENTRIES_VA);
        if (!IsReadableRange(regions, entries, T6_XASSET_ENTRY_SIZE))
            return 0;

        int patched = 0;
        for (uint32_t i = 0; i < T6_XASSET_ENTRY_COUNT; ++i)
        {
            const uintptr_t entry = entries + static_cast<uintptr_t>(i) * T6_XASSET_ENTRY_SIZE;
            uint32_t type = 0;
            uint32_t header = 0;
            if (!TryReadU32(entry, type) || type != T6_ASSET_FONT || !TryReadU32(entry + 4, header) || !header)
                continue;
            if (PatchFontHeader(header, "entry", g_scanPass <= 2))
                ++patched;
        }
        return patched;
    }

    int ApplyFontPool(const std::vector<Region>& regions)
    {
        if (!g_fontPayloadLoaded)
            return 0;

        uint32_t fontPool = 0;
        uint32_t fontCapacity = 0;
        if (!ReadPoolInfo(regions, T6_ASSET_FONT, fontPool, fontCapacity))
            return 0;

        int patched = 0;
        const uint32_t maxCapacity = (std::min)(fontCapacity, 4096u);
        for (uint32_t i = 0; i < maxCapacity; ++i)
        {
            const uintptr_t header = static_cast<uintptr_t>(fontPool) + static_cast<uintptr_t>(i) * 36u;
            if (IsReadableRange(regions, header, 36) && PatchFontHeader(header, "pool-direct", g_scanPass <= 2))
                ++patched;
        }

        if (g_scanPass <= 2)
            Log("font pool info=" + std::to_string(fontPool) + "/" + std::to_string(fontCapacity) + " patched=" + std::to_string(patched));
        return patched;
    }

    int ApplyFontOverrides(const std::vector<Region>& regions)
    {
        return ApplyFontEntryTable(regions);
    }

    int ApplyAll()
    {
        if (!g_payloadLoaded)
            return 0;

        const auto regions = BuildRegions();
        int entryLocalizeCandidates = 0;
        int entryLocalizeMatched = 0;
        int entryStringTableCandidates = 0;
        int entryStringTableMatched = 0;
        int poolLocalizeCandidates = 0;
        int poolLocalizeMatched = 0;
        int poolStringTableCandidates = 0;
        int poolStringTableMatched = 0;

        const int entryPatched = g_textPayloadLoaded
            ? ApplyXAssetEntries(regions, entryLocalizeCandidates, entryLocalizeMatched, entryStringTableCandidates, entryStringTableMatched)
            : 0;
        const int poolPatched = g_textPayloadLoaded
            ? ApplyKnownPools(regions, poolLocalizeCandidates, poolLocalizeMatched, poolStringTableCandidates, poolStringTableMatched)
            : 0;
        const int fontPatched = ApplyFontOverrides(regions);
        const int totalPatched = entryPatched + poolPatched + fontPatched;
        ++g_scanPass;
        if (totalPatched)
        {
            Log("patched total=" + std::to_string(totalPatched) + " entries=" + std::to_string(entryPatched) + " pools="
                + std::to_string(poolPatched) + " fonts=" + std::to_string(fontPatched));
        }
        else if (g_scanPass <= 5 || g_scanPass % 10 == 0)
        {
            Log("scan pass=" + std::to_string(g_scanPass)
                + " entryLocalizeCandidates=" + std::to_string(entryLocalizeCandidates)
                + " entryLocalizeMatched=" + std::to_string(entryLocalizeMatched)
                + " entryStringTableCandidates=" + std::to_string(entryStringTableCandidates)
                + " entryStringTableMatched=" + std::to_string(entryStringTableMatched)
                + " poolLocalizeCandidates=" + std::to_string(poolLocalizeCandidates)
                + " poolLocalizeMatched=" + std::to_string(poolLocalizeMatched)
                + " poolStringTableCandidates=" + std::to_string(poolStringTableCandidates)
                + " poolStringTableMatched=" + std::to_string(poolStringTableMatched));
        }
        return totalPatched;
    }

    DWORD WINAPI WorkerThread(LPVOID)
    {
        if (!IsT6SpProcess())
        {
            Log("not t6sp.exe, worker stopped");
            return 0;
        }

        if (!LoadPayload())
            return 0;

        Log("db_find inline hook disabled; using delayed safe scans");
        Sleep(6000);

        for (int i = 0; i < 600; ++i)
        {
            if (i < 90 || i % 5 == 0)
            {
                ApplyAll();
            }
            Sleep(1000);
        }

        Log("worker finished");
        return 0;
    }
}

extern "C" __declspec(dllexport) int T6TR_ApplyNow()
{
    if (!g_payloadLoaded && !LoadPayload())
        return 0;
    return ApplyAll();
}

extern "C" __declspec(dllexport) DWORD WINAPI XInputGetState(DWORD userIndex, void* state)
{
    using Fn = DWORD(WINAPI*)(DWORD, void*);
    auto fn = reinterpret_cast<Fn>(LoadRealXInputProc("XInputGetState"));
    return fn ? fn(userIndex, state) : ERROR_DEVICE_NOT_CONNECTED;
}

extern "C" __declspec(dllexport) DWORD WINAPI XInputSetState(DWORD userIndex, void* vibration)
{
    using Fn = DWORD(WINAPI*)(DWORD, void*);
    auto fn = reinterpret_cast<Fn>(LoadRealXInputProc("XInputSetState"));
    return fn ? fn(userIndex, vibration) : ERROR_DEVICE_NOT_CONNECTED;
}

extern "C" __declspec(dllexport) DWORD WINAPI XInputGetCapabilities(DWORD userIndex, DWORD flags, void* capabilities)
{
    using Fn = DWORD(WINAPI*)(DWORD, DWORD, void*);
    auto fn = reinterpret_cast<Fn>(LoadRealXInputProc("XInputGetCapabilities"));
    return fn ? fn(userIndex, flags, capabilities) : ERROR_DEVICE_NOT_CONNECTED;
}

extern "C" __declspec(dllexport) void WINAPI XInputEnable(BOOL enable)
{
    using Fn = void(WINAPI*)(BOOL);
    auto fn = reinterpret_cast<Fn>(LoadRealXInputProc("XInputEnable"));
    if (fn)
        fn(enable);
}

extern "C" __declspec(dllexport) DWORD WINAPI XInputGetDSoundAudioDeviceGuids(DWORD userIndex, GUID* renderGuid, GUID* captureGuid)
{
    using Fn = DWORD(WINAPI*)(DWORD, GUID*, GUID*);
    auto fn = reinterpret_cast<Fn>(LoadRealXInputProc("XInputGetDSoundAudioDeviceGuids"));
    return fn ? fn(userIndex, renderGuid, captureGuid) : ERROR_DEVICE_NOT_CONNECTED;
}

extern "C" __declspec(dllexport) DWORD WINAPI XInputGetBatteryInformation(DWORD userIndex, BYTE devType, void* batteryInformation)
{
    using Fn = DWORD(WINAPI*)(DWORD, BYTE, void*);
    auto fn = reinterpret_cast<Fn>(LoadRealXInputProc("XInputGetBatteryInformation"));
    return fn ? fn(userIndex, devType, batteryInformation) : ERROR_DEVICE_NOT_CONNECTED;
}

extern "C" __declspec(dllexport) DWORD WINAPI XInputGetKeystroke(DWORD userIndex, DWORD reserved, void* keystroke)
{
    using Fn = DWORD(WINAPI*)(DWORD, DWORD, void*);
    auto fn = reinterpret_cast<Fn>(LoadRealXInputProc("XInputGetKeystroke"));
    return fn ? fn(userIndex, reserved, keystroke) : ERROR_EMPTY;
}

extern "C" __declspec(dllexport) DWORD WINAPI XInputOrd2(DWORD a, void* b)
{
    using Fn = DWORD(WINAPI*)(DWORD, void*);
    auto fn = reinterpret_cast<Fn>(LoadRealXInputOrdinal(2));
    return fn ? fn(a, b) : XInputGetState(a, b);
}

extern "C" __declspec(dllexport) DWORD WINAPI XInputOrd3(DWORD a, void* b)
{
    using Fn = DWORD(WINAPI*)(DWORD, void*);
    auto fn = reinterpret_cast<Fn>(LoadRealXInputOrdinal(3));
    return fn ? fn(a, b) : XInputSetState(a, b);
}

extern "C" __declspec(dllexport) DWORD WINAPI XInputOrd4(DWORD a, DWORD b, void* c)
{
    using Fn = DWORD(WINAPI*)(DWORD, DWORD, void*);
    auto fn = reinterpret_cast<Fn>(LoadRealXInputOrdinal(4));
    return fn ? fn(a, b, c) : XInputGetCapabilities(a, b, c);
}

BOOL WINAPI DllMain(HINSTANCE instance, DWORD reason, LPVOID)
{
    if (reason == DLL_PROCESS_ATTACH)
    {
        g_module = instance;
        DisableThreadLibraryCalls(instance);
        const HANDLE thread = CreateThread(nullptr, 0, WorkerThread, nullptr, 0, nullptr);
        if (thread)
            CloseHandle(thread);
    }
    return TRUE;
}
