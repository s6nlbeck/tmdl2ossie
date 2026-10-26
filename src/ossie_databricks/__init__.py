# Licensed to the Apache Software Foundation (ASF) under one or more
# contributor license agreements.  See the NOTICE file distributed with
# this work for additional information regarding copyright ownership.
# The ASF licenses this file to You under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with
# the License.  You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Apache Ossie -> Databricks Unity Catalog Metric View (YAML v1.1), offline.

A Python port of the export direction of the Java converter in
``vendor/ossie-converters-main/databricks/java`` (``OssieToMetricView`` plus the
shared parts of ``OssieConverterCommon``). Behavior, error messages and notices
follow the Java implementation; its export test suite is ported to
``tests/test_ossie_databricks.py``. The import direction (Metric View -> Ossie)
is not ported.
"""

from ossie_databricks._common import (
    MV_VERSION,
    OSSIE_VERSION,
    ConversionError,
    Result,
    parse_yaml,
)
from ossie_databricks.ossie_to_metric_view import convert_ossie_to_metric_view

__all__ = [
    "MV_VERSION",
    "OSSIE_VERSION",
    "ConversionError",
    "Result",
    "convert_ossie_to_metric_view",
    "parse_yaml",
]
