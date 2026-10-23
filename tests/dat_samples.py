"""合成 `.dat` 测试夹具模块：提供符合 Campbell Scientific / OBS3A 规范的标准 6 通道表头。

说明：
    所有自动化测试均基于此合成表头动态构造测试观测行，绝不读取或包含任何真实野外观测数据。
"""

HEADER = (
    "OBS3A #0000 Firmware Version: 6.20\n"
    "Sensor                            Data Columns\n"
    " 1 Depth Mean\n"
    " 2 OBS-1 (NTU) Mean\n"
    " 4 Temperature (deg. C) Mean\n"
    " 5 Conductivity(mS/cm) Mean\n"
    " 8 Salinity (PSU) Mean\n"
    " 9 Battery (V) Mean\n"
)
