#!/usr/bin/env python3
"""Generate a small synthetic cardiac-CT DICOM tree with identifiers planted in the places they are known
to leak, for testing anonymise_dicom.py. Usage: make_test_fixtures.py <out_dir>"""
import sys, datetime
from pathlib import Path
import numpy as np
import pydicom
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.sequence import Sequence
from pydicom.uid import generate_uid, ExplicitVRLittleEndian, CTImageStorage

PATIENTS = [
    dict(folder="ORFAN0231", pid="1234567", name="SMITH^JOHN", dob="19610314", sex="M", sdate="20190522"),
    dict(folder="ORFAN0418", pid="7654321", name="JONES^MARY", dob="19750101", sex="F", sdate="20210110"),
]
SERIES = [(3, "DS_StepShoot SOFT TISSUE B30f 75 %", 4), (4, "DS_StepShoot VASCULAR B26f 75 %", 6)]


def ct_image(p, series_no, series_desc, inst, study_uid, series_uid, for_uid):
    fm = FileMetaDataset()
    fm.MediaStorageSOPClassUID = CTImageStorage
    fm.MediaStorageSOPInstanceUID = generate_uid()
    fm.TransferSyntaxUID = ExplicitVRLittleEndian
    fm.ImplementationClassUID = "1.2.276.0.7230010.3.0.3.6.1"
    fm.SendingApplicationEntityTitle = "PACSGW01"
    ds = Dataset()
    ds.file_meta = fm
    ds.is_little_endian = True; ds.is_implicit_VR = False
    ds.SOPClassUID = CTImageStorage
    ds.SOPInstanceUID = fm.MediaStorageSOPInstanceUID
    ds.PatientName = p["name"]; ds.PatientID = p["pid"]; ds.PatientBirthDate = p["dob"]; ds.PatientSex = p["sex"]
    ds.PatientAge = "058Y"; ds.IssuerOfPatientID = "RWE"; ds.OtherPatientIDs = "NHS9998887776"
    ds.PatientAddress = "1 High Street, Sometown"; ds.PatientWeight = "82"
    ds.StudyDate = p["sdate"]; ds.SeriesDate = p["sdate"]; ds.AcquisitionDate = p["sdate"]; ds.ContentDate = p["sdate"]
    ds.StudyTime = "154316"; ds.SeriesTime = "154520"; ds.AcquisitionTime = "154522.12"; ds.ContentTime = "154600"
    ds.AcquisitionDateTime = p["sdate"] + "154522.120000"
    ds.ContrastBolusStartTime = "154025.48"; ds.ContrastBolusAgent = "OMNIPAQUE 350"
    ds.AccessionNumber = "ACC" + p["pid"]; ds.StudyID = "S" + p["pid"]
    ds.Modality = "CT"; ds.Manufacturer = "SIEMENS"; ds.ManufacturerModelName = "SOMATOM Force"
    ds.InstitutionName = "Example Hospital"; ds.InstitutionAddress = "1 Example Road, Sometown"
    ds.StationName = "CT01EXAM"; ds.DeviceSerialNumber = "73470"; ds.SoftwareVersions = "syngo CT VB20A"
    ds.ReferringPhysicianName = "BLOGGS^J^Dr"; ds.OperatorsName = "RADIOG^A"; ds.PhysiciansOfRecord = "EXAMPLE^R"
    ds.StudyDescription = "CT Cardiac angiogram coronary"; ds.SeriesDescription = series_desc; ds.ProtocolName = "STEP_N_SHOOT"
    ds.ImageComments = f"pt {p['name']} seen in clinic"
    ds.StudyInstanceUID = study_uid; ds.SeriesInstanceUID = series_uid; ds.FrameOfReferenceUID = for_uid
    ds.IrradiationEventUID = generate_uid(prefix="1.3.12.2.1107.5.1.4.")
    ds.SeriesNumber = series_no; ds.InstanceNumber = inst; ds.ImageType = ["ORIGINAL", "PRIMARY", "AXIAL"]
    ds.SliceThickness = "0.75"; ds.KVP = "120"; ds.ConvolutionKernel = "B26f"; ds.PatientPosition = "HFS"
    ds.ImagePositionPatient = [-91.28, -286.78, -480.0 - inst * 0.5]; ds.ImageOrientationPatient = [1, 0, 0, 0, 1, 0]
    ds.PixelSpacing = [0.44, 0.44]; ds.RescaleIntercept = "-1024"; ds.RescaleSlope = "1"; ds.RescaleType = "HU"
    ds.WindowCenter = "200"; ds.WindowWidth = "600"
    # sequences that carry codes / UIDs / dates
    pc = Dataset(); pc.CodeValue = "CACRY"; pc.CodingSchemeDesignator = "L"; pc.CodeMeaning = "CT coronary angio"
    ds.ProcedureCodeSequence = Sequence([pc])
    ri = Dataset(); ri.ReferencedSOPClassUID = CTImageStorage; ri.ReferencedSOPInstanceUID = generate_uid()
    ds.ReferencedImageSequence = Sequence([ri])
    ra = Dataset(); ra.RequestedProcedureID = "REQ" + p["pid"]; ra.ScheduledProcedureStepStartDate = p["sdate"]
    ds.RequestAttributesSequence = Sequence([ra])
    # private tags mimicking the leaks seen in real PACS exports
    ds.add_new((0x07A3, 0x0010), "LO", "ELSCINT1")
    ds.add_new((0x07A3, 0x1019), "UN", b"BLOGGS J Dr 1234567")
    ds.add_new((0x07A3, 0x1034), "SH", "20403")
    ds.add_new((0x07A5, 0x0010), "LO", "ELSCINT1")
    ds.add_new((0x07A5, 0x1054), "UN", (p["sdate"] + "171700.000000 ").encode())
    ds.add_new((0x0029, 0x0010), "LO", "SIEMENS CSA HEADER")
    ds.add_new((0x0029, 0x1010), "OB", b"SV10" + b"\x00" * 64)
    # pixels
    ds.Rows = 64; ds.Columns = 64; ds.SamplesPerPixel = 1; ds.PhotometricInterpretation = "MONOCHROME2"
    ds.BitsAllocated = 16; ds.BitsStored = 12; ds.HighBit = 11; ds.PixelRepresentation = 0
    rng = np.random.default_rng(inst)
    ds.PixelData = rng.integers(0, 2000, (64, 64), dtype=np.uint16).tobytes()
    return ds


def dose_sr(p, study_uid):
    fm = FileMetaDataset()
    fm.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.88.67"
    fm.MediaStorageSOPInstanceUID = generate_uid(); fm.TransferSyntaxUID = ExplicitVRLittleEndian
    fm.ImplementationClassUID = "1.2.276.0.7230010.3.0.3.6.1"
    ds = Dataset(); ds.file_meta = fm; ds.is_little_endian = True; ds.is_implicit_VR = False
    ds.SOPClassUID = fm.MediaStorageSOPClassUID; ds.SOPInstanceUID = fm.MediaStorageSOPInstanceUID
    ds.PatientName = p["name"]; ds.PatientID = p["pid"]; ds.PatientBirthDate = p["dob"]; ds.PatientSex = p["sex"]
    ds.StudyDate = p["sdate"]; ds.StudyTime = "154316"; ds.Modality = "SR"; ds.SeriesNumber = 501; ds.InstanceNumber = 1
    ds.StudyInstanceUID = study_uid; ds.SeriesInstanceUID = generate_uid(); ds.Manufacturer = "SIEMENS"
    ds.CompletionFlag = "COMPLETE"; ds.VerificationFlag = "UNVERIFIED"
    c = Dataset(); c.ValueType = "TEXT"; c.TextValue = f"Dose report for {p['name']} {p['pid']}"
    ds.ContentSequence = Sequence([c])
    return ds


def us_image(p, study_uid):
    """An ultrasound frame: text-prone modality, quarantined by the tag policy; carries the same identifiers."""
    fm = FileMetaDataset()
    fm.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.6.1"  # Ultrasound Image Storage
    fm.MediaStorageSOPInstanceUID = generate_uid(); fm.TransferSyntaxUID = ExplicitVRLittleEndian
    fm.ImplementationClassUID = "1.2.276.0.7230010.3.0.3.6.1"
    ds = Dataset(); ds.file_meta = fm; ds.is_little_endian = True; ds.is_implicit_VR = False
    ds.SOPClassUID = fm.MediaStorageSOPClassUID; ds.SOPInstanceUID = fm.MediaStorageSOPInstanceUID
    ds.PatientName = p["name"]; ds.PatientID = p["pid"]; ds.PatientBirthDate = p["dob"]; ds.PatientSex = p["sex"]
    ds.StudyDate = p["sdate"]; ds.StudyTime = "160000"; ds.Modality = "US"; ds.SeriesNumber = 7; ds.InstanceNumber = 1
    ds.StudyInstanceUID = study_uid; ds.SeriesInstanceUID = generate_uid(); ds.Manufacturer = "PHILIPS"
    ds.ImageType = ["ORIGINAL", "PRIMARY"]; ds.SeriesDescription = "Echo apical 4ch"
    ds.Rows = 64; ds.Columns = 64; ds.SamplesPerPixel = 1; ds.PhotometricInterpretation = "MONOCHROME2"
    ds.BitsAllocated = 8; ds.BitsStored = 8; ds.HighBit = 7; ds.PixelRepresentation = 0
    ds.PixelData = np.random.default_rng(7).integers(0, 255, (64, 64), dtype=np.uint8).tobytes()
    return ds


def main(out):
    out = Path(out)
    for p in PATIENTS:
        d = out / p["folder"]; d.mkdir(parents=True, exist_ok=True)
        study_uid = generate_uid(prefix="1.3.12.2.1107.5.1.4."); for_uid = generate_uid(prefix="1.3.12.2.1107.5.1.4.")
        n = 0
        for sno, sdesc, count in SERIES:
            suid = generate_uid(prefix="1.3.12.2.1107.5.1.4.")
            for i in range(1, count + 1):
                ds = ct_image(p, sno, sdesc, i, study_uid, suid, for_uid)
                ds.save_as(str(d / f"IM-{sno:04d}-{i:04d}.dcm"), write_like_original=False); n += 1
        dose_sr(p, study_uid).save_as(str(d / "SR-0501-0001.dcm"), write_like_original=False); n += 1
        if p["folder"] == "ORFAN0418":   # the second patient also has an ultrasound frame (quarantined by modality)
            us_image(p, study_uid).save_as(str(d / "US-0007-0001.dcm"), write_like_original=False); n += 1
        (d / "notes.txt").write_text("not a dicom file")
        print(f"{p['folder']}: {n} DICOM files")
    with open(out / "mapping.csv", "w") as fh:
        fh.write("Hospital number,CBB ID\n")
        for i, p in enumerate(PATIENTS, 1):
            fh.write(f"{p['pid']},CBB{400+i:04d}\n")
    with open(out / "mapping_by_folder.csv", "w") as fh:
        fh.write("ORFAN ID,CBB ID\n")
        for i, p in enumerate(PATIENTS, 1):
            fh.write(f"{p['folder']},CBB{400+i:04d}\n")
        fh.write("ORFAN0999,CBB0499\n")  # in the mapping but no scans on disk
    print("mapping.csv / mapping_by_folder.csv written")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "test_fixtures")
