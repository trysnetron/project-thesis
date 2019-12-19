import argparse
import yaml
import os
import sys
import re
import math
import logging

logging.basicConfig(format='%(levelname)s:%(message)s', level=logging.INFO)
logger = logging.getLogger("bus_load_calc")

def get_all_subjects_from_namespace(source, namespace):

    fixed_subject_id_pattern = re.compile(r"#define \w+_FIXED_SUBJECT_ID")

    subjects = []

    for root, _, files in os.walk(os.path.join(source, namespace), topdown=True):
        for file_ in files:
            file_path = os.path.join(root, file_)
            with open(file_path, "r") as f:
                if fixed_subject_id_pattern.search(f.read()):
                    # Either service or message with non fixed subject id
                    subjects.append(file_path)
    return subjects

class Message:
    def __init__(self, *args, **kwargs):
        self._wc_bus_load = 0
        self._bc_bus_load = 0
        self._frames = []
        self._wctts = []
        self._bctts = []
        self._frequency = -1
        self._mtu = 64
    
    @classmethod
    def calculate_next_dlc(self, length):
        CAN_FD_DLC = [0, 1, 2, 3 ,4 ,5 ,6 ,7, 8, 12, 16, 20, 24, 32, 48, 64]
        for i in range(len(CAN_FD_DLC)):
            if length <= CAN_FD_DLC[i]:
                return CAN_FD_DLC[i]

    def _calculate_wctt(self, t_arb, t_data):
        for p in self._frames:
            wctt = 32 * t_arb + (28 + 5* math.ceil((p - 16)/64) + 10*p) * t_data
            self._wctts.append(wctt)

    def _calculate_bctt(self, t_arb, t_data):
        for p in self._frames:
            bctt = 29 * t_arb + (27 + 5* math.ceil((p - 16)/64) + 8*p) * t_data
            self._bctts.append(bctt)
    
    def calculate_busload(self, t_arb, t_data):
        self._calculate_wctt(t_arb, t_data)
        self._calculate_bctt(t_arb, t_data)
        
        for wctt in self._wctts:
            self._wc_bus_load += wctt * self._frequency * 100

        for bctt in self._bctts:
            self._bc_bus_load += bctt * self._frequency * 100
    
    def calculate_payload_per_frame(self):
        pass

class UavcanMessage(Message):
    def __init__(self, path):
        super().__init__()
        self._path = path
        self._name = self._get_subject_name()
        self._payload = self._get_max_payload_length()
        self._frames = []
        self._wctts = []
        self._frequency = -1
        self._bus_load = 0
    
    def _get_subject_name(self):
        """ Return the full name for a subject"""
        subject_name_pattern = re.compile(r"#define \w+_NAME +\"([a-zA-Z0-9_.]+)\"")
        with open(self._path, "r") as f:
            return subject_name_pattern.findall(f.read())[0]

    def _get_max_payload_length(self):
        """ Return the number of Bytes of payload for a given subject"""
        subject_name_pattern = re.compile(r"#define \w+_MAX_SIZE +([0-9\\(\\)\\+\\/ ]*)")
        with open(self._path, "r") as f:
            return int(eval(subject_name_pattern.findall(f.read())[0]))

    def calculate_payload_per_frame(self):
        """ 
        Return a list containing the payload of each frame needed to transmit
        the user payload.
        This includes:
            - 1 Byte UAVCAN tailbyte per CAN(-FD) frame
            - 2 Byte CRC for multiframe UAVCAN messages
            - Padding bytes to get to closest DLC for CAN-FD
        """
        payloads = []
        current_payload = 0
        user_payload = self._payload

        # Use a greedy apporach to fill one and one frame
        while user_payload > 0:

            if user_payload > (self._mtu - 1):
                current_payload += (self._mtu - 1)
            else:
                current_payload += user_payload

            # Decrement the total number of Bytes left of user payload
            user_payload -= current_payload

            # Add tailbyte
            current_payload += 1

            if user_payload == 0 and len(payloads) == 0: # Singleframe

                # Need to pad until next valid DLC
                payloads.append(self.calculate_next_dlc(current_payload))
            elif user_payload == 0 and len(payloads) > 0: # Multiframe
                if current_payload < (self._mtu - 1): # The two CRC bytes fits into the last multiframe
                    
                    current_payload += 2
                    payloads.append(self.calculate_next_dlc(current_payload))
                elif current_payload == (self._mtu - 1): # The CRC must be split over two frames
                    
                    # Append one of the two CRC bytes to the current frame
                    current_payload += 1
                    
                    #  Append the last full frame
                    payloads.append(self.calculate_next_dlc(current_payload))

                    # Append the last frame containing 1 Byte CRC and tailbyte
                    payloads.append(2)
                else: # Both CRC bytes must be put in new frame
                    
                    payloads.append(self.calculate_next_dlc(current_payload))

                    # Append the last frame containing 2 Byte CRC and tailbyte
                    payloads.append(3)
            else: # Middle of multiframe
                payloads.append(self.calculate_next_dlc(current_payload)) 

            # Reset payload for next frame
            current_payload = 0

        self._frames = payloads

class RevolveProtocolMessage(Message):
    def __init__(self, name, frequency=None, size=None):
        super().__init__()
        self._name = name
        self._frequency = frequency
        self._size = size
    
    def calculate_payload_per_frame(self):
        assert self._size <= self._mtu

        self._frames.append(self.calculate_next_dlc(self._size))
    
def update_frequencies(messages, default_frequency, user_registered_frequencies):
    subjects = dict((message._name, message) for message in messages)
    # print(subjects.values())
    for subject, freq in user_registered_frequencies.items():
        if subject not in subjects:
            logger.error(f"Got unknown subject when registering frequencies: {subject}")
            sys.exit(1)
        
        subjects[subject]._frequency = freq

    # Log a warning if a frequency is not set
    for name, subject in subjects.items():
        if subject._frequency == -1:
            logger.warning(f"No frequency was found for '{subject._name}', is set to default value '{default_frequency}'")
            subjects[name]._frequency = default_frequency

    return list(subjects.values())
    

    

        
def main():
    parser = argparse.ArgumentParser()

    parser.add_argument("-c",
                        "--config",
                        dest="config_path",
                        required=True,
                        help="Path to config file")

    args = parser.parse_args()

    config_file_path = os.path.abspath(args.config_path)
    
    with open(config_file_path, "r") as f:
        config = yaml.safe_load(f)
    
    # MTU size
    mtu = config["general"]["MTU"]

    # Default frequency for signals
    default_freq = config["general"]["default_frequency"]

    # Get the time periods for both arbitration and data phase
    t_arb = 1/float(config["general"]["arbitration_bit_rate"])
    t_data = 1/float(config["general"]["data_bit_rate"])

    # Get source path of generated C files
    source = os.path.abspath(os.path.join(os.path.dirname(config_file_path), config["uavcan"]["source"]))

    # Extract a flattened list of all periodic UAVCAN subjects
    subjects_paths = []
    for namespace in config["uavcan"]["namespaces"]:
        subjects_paths.extend(get_all_subjects_from_namespace(source, namespace))
    
    subjects = []
    for path in subjects_paths:
        subjects.append(UavcanMessage(path))
    

    subjects = update_frequencies(subjects, default_freq, config["uavcan"]["frequencies"])
    # RP messages
    for message, value in config["RevolveProtocol"].items():
        subjects.extend([RevolveProtocolMessage(message + "_" + str(i), frequency=value["frequency"], size=value["size"]) for i in range(value["amount"])])

    logger.info("Found the following messages")
    
    worst_case_bus_load = 0
    best_case_bus_load = 0

    longest_subject_name = len(max(subjects, key=lambda x: len(x._name))._name)

    for subject in subjects:
        subject.calculate_payload_per_frame()
        subject.calculate_busload(t_arb, t_data)
        worst_case_bus_load += subject._wc_bus_load
        best_case_bus_load += subject._bc_bus_load
        logger.info(f"\t - {subject._name:<{longest_subject_name}}: Frequency: {subject._frequency:>3}, Worst Case Busload: {subject._wc_bus_load:>5.2e}, Best Case Busload: {subject._bc_bus_load:>5.2e}")

    
    

    logger.info("")
    
    logger.info(f"Total worst case bus load: {worst_case_bus_load:.2f}")
    logger.info(f"Total best case bus load: {best_case_bus_load:.2f}")
    

    



if __name__ == "__main__":
    main()