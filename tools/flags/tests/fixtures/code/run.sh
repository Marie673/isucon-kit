#!/bin/sh
exec app --workers "${ISU_SH_WORKERS:-4}"
